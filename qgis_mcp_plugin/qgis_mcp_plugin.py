import os
import ast
import base64
import configparser
import contextlib
import io
import json
import math
import socket
import struct
import time
import traceback
import sys
from qgis.core import *
from qgis.gui import *
# Bind the package itself so snippets can reach qgis.core/qgis.utils. Must come
# after the star imports, which would otherwise shadow the name.
import qgis
import qgis.core
import qgis.gui
from qgis.PyQt.QtCore import QByteArray, QBuffer, QEventLoop, QIODevice, QObject, pyqtSignal, QTimer, Qt, QSize, QVariant
from qgis.PyQt.QtWidgets import QAction, QCheckBox, QDockWidget, QVBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QWidget
from qgis.PyQt.QtGui import QIcon, QColor
from datetime import datetime

# Filename used when compiling user snippets, so tracebacks name the source.
_EXEC_FILENAME = "<qgis_mcp>"

# Cap on any single string field sent back to the client. A print() inside a
# loop over a large layer would otherwise flood the caller's context.
_MAX_OUTPUT_CHARS = 20_000

# The socket protocol is deliberately versioned because the QGIS plugin and
# MCP server are installed separately and must be updated together.
_PROTOCOL_VERSION = 1
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
_FRAME_HEADER = struct.Struct(">I")

# QGIS 4 / Qt 6 moved several long-standing enum members into scoped enums.
try:
    _LAYER_VECTOR = Qgis.LayerType.Vector
    _LAYER_RASTER = Qgis.LayerType.Raster
except AttributeError:
    _LAYER_VECTOR = QgsMapLayer.VectorLayer
    _LAYER_RASTER = QgsMapLayer.RasterLayer

try:
    _GEOMETRY_POINT = Qgis.GeometryType.Point
except AttributeError:
    _GEOMETRY_POINT = QgsWkbTypes.PointGeometry

try:
    _MESSAGE_CRITICAL = Qgis.MessageLevel.Critical
    _MESSAGE_INFO = Qgis.MessageLevel.Info
except AttributeError:
    _MESSAGE_CRITICAL = Qgis.Critical
    _MESSAGE_INFO = Qgis.Info

try:
    _MAP_ANTIALIASING = Qgis.MapSettingsFlag.Antialiasing
    _MAP_DRAW_LABELING = Qgis.MapSettingsFlag.DrawLabeling
    _MAP_ADVANCED_EFFECTS = Qgis.MapSettingsFlag.UseAdvancedEffects
except AttributeError:
    _MAP_ANTIALIASING = QgsMapSettings.Antialiasing
    _MAP_DRAW_LABELING = QgsMapSettings.DrawLabeling
    _MAP_ADVANCED_EFFECTS = QgsMapSettings.UseAdvancedEffects

try:
    _RIGHT_DOCK_AREA = Qt.DockWidgetArea.RightDockWidgetArea
    _ISO_DATE = Qt.DateFormat.ISODate
except AttributeError:
    _RIGHT_DOCK_AREA = Qt.RightDockWidgetArea
    _ISO_DATE = Qt.ISODate


def _plugin_version():
    metadata = configparser.ConfigParser()
    metadata.read(os.path.join(os.path.dirname(__file__), "metadata.txt"))
    return metadata.get("general", "version", fallback="unknown")


_PLUGIN_VERSION = _plugin_version()
_BRAND_NAME = "WAI QGIS MCP"

# Sentinel: the snippet ended in a statement, so there is no value at all
# (distinct from a final expression that evaluated to None).
_NOTHING = object()


def _truncate(text, limit=_MAX_OUTPUT_CHARS):
    """Clip *text*, marking the clip so the reader knows it is a prefix."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def _dbg(msg: str):
    """Write privacy-safe diagnostics to the QGIS log when opted in."""
    try:
        enabled = QgsSettings().value("qgis_mcp/diagnostics", False, type=bool)
        if enabled:
            QgsMessageLog.logMessage(str(msg), _BRAND_NAME, _MESSAGE_INFO)
    except Exception:
        # Diagnostics must never interfere with command processing.
        pass

class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""
    client_count_changed = pyqtSignal(int)
    RENDER_TIMEOUT_SECONDS = 55
    
    def __init__(self, host='localhost', port=9876, iface=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.running = False
        self.socket = None
        self.last_error = None
        # Each non-blocking client has independent receive and transmit queues.
        self.clients = {}
        self.timer = None
    
    def start(self):
        """Start the server"""
        self.last_error = None
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        _dbg(f"Binding MCP server socket on {self.host}:{self.port}")
        try:
            self.socket.bind((self.host, self.port))
            # Allow a reasonable backlog so several peers can queue while we are busy
            self.socket.listen(20)
            self.socket.setblocking(False)
            
            # Create a timer to process server operations
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_server)
            self.timer.start(25)
            
            _dbg("Server started and listening")
            return True
        except Exception as e:
            self.last_error = str(e)
            QgsMessageLog.logMessage(
                f"Failed to start server: {self.last_error}",
                _BRAND_NAME,
                _MESSAGE_CRITICAL,
            )
            self.stop()
            return False
            
    def stop(self):
        """Stop the server"""
        self.running = False
        
        if self.timer:
            _dbg("Stopping server timer")
            self.timer.stop()
            self.timer = None
            
        # Close server socket first so no more accepts come in
        if self.socket:
            self.socket.close()
        
        # Close all connected client sockets
        for cli in list(self.clients.keys()):
            try:
                cli.close()
            except Exception:
                pass
            self.clients.pop(cli, None)
            
        self.socket = None
        self.client_count_changed.emit(0)
        _dbg("Server stopped")
    
    def process_server(self):
        """Process server operations (called by timer)

        Runs inside the main QGIS GUI thread (triggered by QTimer).  The logic
        is fully non-blocking:

        1. Accept any pending incoming TCP connections and register them.
        2. Iterate over all registered client sockets, read whatever data is
           available, and try to decode a complete JSON message.
        3. Execute the command and send the reply back through the very same
           socket.  When the peer closes the connection (recv → b""), drop it.
        """
        if not self.running:
            return

        # ------------------------------------------------------------------
        # 1) Accept all waiting peers
        # ------------------------------------------------------------------
        while True:
            try:
                client, address = self.socket.accept()
                client.setblocking(False)
                self.clients[client] = {"rx": b"", "tx": bytearray()}
                self.client_count_changed.emit(len(self.clients))
                _dbg(f"Client connected; active clients = {len(self.clients)}")
            except BlockingIOError:
                break  # No more queued connections
            except Exception as e:
                _dbg(f"Error accepting connection: {str(e)}")
                break

        # ------------------------------------------------------------------
        # 2) Handle data from existing clients
        # ------------------------------------------------------------------
        for cli in list(self.clients.keys()):  # iterate over a copy – may mutate
            try:
                data = cli.recv(65536)
                if data:
                    state = self.clients[cli]
                    state["rx"] += data
                    if len(state["rx"]) > _MAX_MESSAGE_BYTES + _FRAME_HEADER.size:
                        raise ValueError("Request exceeded the 32 MB message limit")
                else:
                    # Peer has closed the connection
                    _dbg("Client disconnected")
                    self._drop_client(cli)
                    continue
            except BlockingIOError:
                # No data ready for this socket this tick
                pass
            except Exception as e:
                _dbg(f"Error receiving data: {str(e)} – dropping client")
                self._drop_client(cli)
                continue

            state = self.clients.get(cli)
            if not state:
                continue

            # Process every complete length-prefixed request in the buffer.
            while len(state["rx"]) >= _FRAME_HEADER.size:
                message_length = _FRAME_HEADER.unpack(state["rx"][:4])[0]
                if message_length > _MAX_MESSAGE_BYTES:
                    self._drop_client(cli)
                    break
                frame_length = _FRAME_HEADER.size + message_length
                if len(state["rx"]) < frame_length:
                    break
                payload = state["rx"][4:frame_length]
                state["rx"] = state["rx"][frame_length:]
                try:
                    command = json.loads(payload.decode("utf-8"))
                    response = self.execute_command(command)
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    response = {"status": "error", "message": f"Invalid request JSON: {e}"}
                self._queue_response(cli, response)

            # Flush as much queued output as the non-blocking socket accepts.
            state = self.clients.get(cli)
            if state and state["tx"]:
                try:
                    sent = cli.send(state["tx"])
                    del state["tx"][:sent]
                except BlockingIOError:
                    pass
                except Exception as e:
                    _dbg(f"Error sending response: {str(e)} – dropping client")
                    self._drop_client(cli)

    def execute_command(self, command):
        """Execute a command"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            started = time.monotonic()
            _dbg(f"Command started: {cmd_type}")
            
            # "ping" is kept for socket-level liveness checks even though the
            # MCP server exposes no public ping tool.
            handlers = {
                "ping": self.ping,
                "get_project": self.get_project,
                "execute_code": self.execute_code,
                "get_layers": self.get_layers,
                "get_layer_features": self.get_layer_features,
                "render_map": self.render_map,
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    result = handler(**params)
                    elapsed_ms = (time.monotonic() - started) * 1000
                    if (
                        cmd_type == "execute_code"
                        and isinstance(result, dict)
                        and result.get("success") is False
                    ):
                        _dbg(
                            f"Command failed: {cmd_type} ({elapsed_ms:.1f} ms): "
                            f"{result.get('error', 'execution error')}"
                        )
                    else:
                        _dbg(f"Command completed: {cmd_type} ({elapsed_ms:.1f} ms)")
                    return {"status": "success", "result": result}
                except Exception as e:
                    elapsed_ms = (time.monotonic() - started) * 1000
                    _dbg(
                        f"Command failed: {cmd_type} ({elapsed_ms:.1f} ms): "
                        f"{type(e).__name__}: {e}"
                    )
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
                
        except Exception as e:
            _dbg(f"Error executing command: {str(e)}")
            return {"status": "error", "message": str(e)}
    
    # Command handlers
    def ping(self, **kwargs):
        """Return compatibility information for the MCP server handshake."""
        return {
            "pong": True,
            "protocol_version": _PROTOCOL_VERSION,
            "plugin_version": _PLUGIN_VERSION,
        }

    def get_project(self, **kwargs):
        """Return bounded metadata about the current project and map canvas."""
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        active_layer = self.iface.activeLayer()
        layers = list(project.mapLayers().values())
        visible_layer_count = 0
        for layer in layers:
            node = root.findLayer(layer.id())
            if node is not None and node.isVisible():
                visible_layer_count += 1

        return {
            "file_name": project.fileName(),
            "title": project.title(),
            "dirty": project.isDirty(),
            "home_path": project.homePath(),
            "crs": project.crs().authid(),
            "layer_count": len(layers),
            "visible_layer_count": visible_layer_count,
            "active_layer": (
                {"id": active_layer.id(), "name": active_layer.name()}
                if active_layer is not None
                else None
            ),
            "canvas": {
                "crs": canvas.mapSettings().destinationCrs().authid(),
                "extent": {
                    "xmin": extent.xMinimum(),
                    "ymin": extent.yMinimum(),
                    "xmax": extent.xMaximum(),
                    "ymax": extent.yMaximum(),
                },
                "scale": canvas.scale(),
                "rotation": canvas.rotation(),
            },
            "integration": {
                "plugin_version": _PLUGIN_VERSION,
                "protocol_version": _PROTOCOL_VERSION,
            },
        }
    
    def _get_layer_type(self, layer):
        """Helper to get layer type as string"""
        if layer.type() == _LAYER_VECTOR:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == _LAYER_RASTER:
            return "raster"
        else:
            return str(layer.type())
    
    def _build_exec_namespace(self):
        """Assemble the namespace that user snippets are executed in."""
        namespace = {}
        for module in (qgis.core, qgis.gui):
            namespace.update(
                {k: v for k, v in vars(module).items() if not k.startswith("_")}
            )

        namespace.update({
            "qgis": qgis,
            "iface": self.iface,
            "json": json,
            "math": math,
            "os": os,
        })

        # `processing` only exists once the Processing plugin has initialised.
        try:
            import processing
            namespace["processing"] = processing
        except ImportError:
            pass

        return namespace

    def execute_code(self, code, **kwargs):
        """Execute arbitrary PyQGIS code with a trailing-expression result.

        The snippet is executed as-is.  If its final statement is a bare
        expression, that expression is evaluated and its value becomes the
        ``result``. Every call receives a fresh namespace; variables do not
        persist between calls::

            layer = QgsProject.instance().mapLayersByName("roads")[0]
            layer.featureCount()

        Anything the snippet prints is captured.  Errors are reported in the
        payload rather than raised, so the caller always sees the traceback
        alongside whatever output was produced before the failure.

        Returns a dict with keys::

            success      bool
            result       the JSON-encodable value of the final expression, else None
            result_repr  repr() of the value when it is not JSON-encodable
            stdout       captured stdout (truncated)
            stderr       captured stderr (truncated)
            error        exception message, when success is False
            traceback    formatted traceback, when success is False
        """
        namespace = self._build_exec_namespace()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        result = _NOTHING

        try:
            # Parse first: a SyntaxError here has no user frames to report.
            tree = ast.parse(code, filename=_EXEC_FILENAME, mode="exec")

            # Split off a trailing bare expression so we can eval it for a value.
            body, tail = tree.body, None
            if body and isinstance(body[-1], ast.Expr):
                body, tail = body[:-1], body[-1]

            # redirect_* restores the originals even if the snippet calls
            # sys.exit(); QGIS's real stdout must never be left dangling.
            with contextlib.redirect_stdout(stdout_capture), \
                    contextlib.redirect_stderr(stderr_capture):
                if body:
                    exec(compile(ast.Module(body=body, type_ignores=[]),
                                 _EXEC_FILENAME, "exec"), namespace)
                if tail is not None:
                    result = eval(compile(ast.Expression(body=tail.value),
                                          _EXEC_FILENAME, "eval"), namespace)
        except SyntaxError as e:
            # No user frames ran; SyntaxError carries its own line/caret.
            return self._exec_failure(e, stdout_capture, stderr_capture, tb=None)
        except BaseException as e:
            # BaseException, not Exception: a snippet calling sys.exit() raises
            # SystemExit, which would otherwise tear down QGIS itself.
            return self._exec_failure(e, stdout_capture, stderr_capture)

        payload = {
            "success": True,
            "result": None,
            "stdout": _truncate(stdout_capture.getvalue()),
            "stderr": _truncate(stderr_capture.getvalue()),
        }
        if result is not _NOTHING:
            payload.update(self._encode_result(result))
        return payload

    def _encode_result(self, result):
        """JSON-encode a snippet's value, falling back to repr()."""
        try:
            json.dumps(result)
            return {"result": result}
        except (TypeError, ValueError):
            pass

        # A QgsVectorLayer's repr is informative; a failed repr is not fatal.
        try:
            rendered = repr(result)
        except Exception as e:
            rendered = f"<unreprable {type(result).__name__}: {e}>"
        return {"result": None, "result_repr": _truncate(rendered)}

    def _exec_failure(self, exc, stdout_capture, stderr_capture, tb=_NOTHING):
        """Build the payload for a snippet that raised."""
        if tb is _NOTHING:
            # Frame 0 is execute_code itself; the model only cares about its code.
            tb = exc.__traceback__.tb_next if exc.__traceback__ else None
        formatted = "".join(
            traceback.format_exception(type(exc), exc, tb)
        )
        return {
            "success": False,
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": _truncate(formatted),
            "stdout": _truncate(stdout_capture.getvalue()),
            "stderr": _truncate(stderr_capture.getvalue()),
        }
    
    def get_layers(self, **kwargs):
        """Get all layers in the project

        Returns a list of layer dictionaries with the keys:

        • id – internal QGIS layer id
        • name – layer name as shown in the layer panel
        • type – "vector_<geom>" or "raster"
        • crs – layer Coordinate Reference System auth id (e.g. "EPSG:4326")
        • fields – attribute field names (vector layers only)
        • visible – boolean layer tree visibility flag
        • feature_count / geometry_type – vector-specific metadata
        • width / height – raster-specific metadata
        """
        project = QgsProject.instance()
        layers = []
        
        for layer_id, layer in project.mapLayers().items():
            # Safely extract attribute field names only for vector layers
            if layer.type() == _LAYER_VECTOR:
                field_names = [str(field.name()) for field in layer.fields()]
            else:
                field_names = []

            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "fields": field_names,
                "visible": project.layerTreeRoot().findLayer(layer_id).isVisible(),
                "crs": layer.crs().authid() if hasattr(layer, "crs") else None,
            }
            
            # Add type-specific information
            if layer.type() == _LAYER_VECTOR:
                layer_info.update({
                    "feature_count": layer.featureCount(),
                    "geometry_type": layer.geometryType()
                })
            elif layer.type() == _LAYER_RASTER:
                layer_info.update({
                    "width": layer.width(),
                    "height": layer.height()
                })
                
            layers.append(layer_info)
        
        return layers
    
    def get_layer_features(self, layer_id, limit=10, **kwargs):
        """Get features from a vector layer"""
        project = QgsProject.instance()
        _dbg(f"Reading up to {limit} features from a vector layer")
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            
            if layer.type() != _LAYER_VECTOR:
                raise Exception(f"Layer is not a vector layer: {layer_id}")
            features = []
            for i, feature in enumerate(layer.getFeatures()):
                if i >= limit:
                    break
                # Extract attributes
                attrs = {}
                for field in layer.fields():
                    attrs[str(field.name())] = str(feature.attribute(field.name()))
                # Extract geometry – full geometry for points, else centroid & bbox
                geom = None
                if feature.hasGeometry():
                    geom_obj = feature.geometry()
                    try:
                        # If the geometry is a point (or multipoint with exactly one point), return full WKT
                        if geom_obj.type() == _GEOMETRY_POINT:
                            geom = {
                                "type": "point",
                                "wkt": str(geom_obj.asWkt(precision=4))
                            }
                        else:
                            # For non-point geometries return centroid and bounding box
                            centroid = geom_obj.centroid()
                            bbox = geom_obj.boundingBox()
                            geom = {
                                "type": "centroid_bbox",
                                "centroid_wkt": str(centroid.asWkt(precision=4)) if centroid and not centroid.isNull() else None,
                                "bbox": {
                                    "xmin": bbox.xMinimum(),
                                    "ymin": bbox.yMinimum(),
                                    "xmax": bbox.xMaximum(),
                                    "ymax": bbox.yMaximum()
                                }
                            }
                    except Exception as e:
                        # Fallback – return geometry as string to avoid breaking the whole call
                        geom = {"error": f"Geometry processing error: {str(e)}"}
                features.append({
                    "id": feature.id(),
                    "attributes": attrs,
                    "geometry": geom
                })
            
            return {
                "layer_id": layer_id,
                "feature_count": layer.featureCount(),
                "features": features,
                "fields": [str(field.name()) for field in layer.fields()]
            }
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
    def render_map(self, path=None, width=800, height=600, include_hidden=False, transparent=False, **kwargs):
        """
        Render the current map view to an image, respecting layer-tree order & visibility.
        """
        try:
            width, height = int(width), int(height)
            if not (1 <= width <= 2048 and 1 <= height <= 2048):
                raise Exception("Width and height must each be between 1 and 2048 pixels")
            project = QgsProject.instance()
            root = project.layerTreeRoot()

            # --- 1) Build layer list in DRAW ORDER (bottom→top), respecting visibility ---
            # QGIS draws layers in the order provided; we want the project's layer-tree order.
            ordered_layers = list(root.layerOrder())  # returns [QgsMapLayer, ...] bottom→top
            if not include_hidden:
                visible_ids = {n.layerId() for n in root.findLayers() if n.isVisible()}
                ordered_layers = [lyr for lyr in ordered_layers if lyr and lyr.id() in visible_ids]

            # Safety: drop invalid layers
            ordered_layers = [lyr for lyr in ordered_layers if lyr and lyr.isValid()]
            if not ordered_layers:
                raise Exception("No drawable layers (after visibility/order filtering).")

            # --- 2) Mirror canvas/map settings so reprojection & styles match what you see ---
            canvas_ms = self.iface.mapCanvas().mapSettings()

            ms = QgsMapSettings()
            ms.setLayers(ordered_layers)
            ms.setExtent(self.iface.mapCanvas().extent())  # current view
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(0, 0, 0, 0) if transparent else QColor(255, 255, 255))
            ms.setOutputDpi(canvas_ms.outputDpi())
            ms.setTransformContext(project.transformContext())
            ms.setDestinationCrs(canvas_ms.destinationCrs())
            ms.setFlag(_MAP_ANTIALIASING, True)
            ms.setFlag(_MAP_DRAW_LABELING, True)  # labels, if any
            ms.setFlag(_MAP_ADVANCED_EFFECTS, True)  # blend modes, layer opacity, etc.

            # --- 3) Render ---
            job = QgsMapRendererParallelJob(ms)
            loop = QEventLoop()
            timed_out = []
            job.finished.connect(loop.quit)
            timeout_timer = QTimer()
            timeout_timer.setSingleShot(True)
            timeout_timer.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
            timeout_timer.start(self.RENDER_TIMEOUT_SECONDS * 1000)
            job.start()
            loop.exec()
            timeout_timer.stop()
            if timed_out:
                job.cancelWithoutBlocking()
                job.waitForFinished()
                raise Exception(f"Render timed out after {self.RENDER_TIMEOUT_SECONDS} seconds")

            img = job.renderedImage()
            if path and not img.save(path):
                raise Exception(f"Failed to save rendered image to {path}")

            encoded = QByteArray()
            buffer = QBuffer(encoded)
            write_only = (
                QIODevice.WriteOnly
                if hasattr(QIODevice, "WriteOnly")
                else QIODevice.OpenModeFlag.WriteOnly
            )
            if not buffer.open(write_only) or not img.save(buffer, "PNG"):
                raise Exception("Failed to encode rendered image as PNG")
            buffer.close()

            return {"rendered": True, "path": path, "width": width, "height": height,
                    "mime_type": "image/png",
                    "image_base64": base64.b64encode(bytes(encoded)).decode("ascii"),
                    "layers_drawn": [lyr.name() for lyr in ordered_layers]}

        except Exception as e:
            raise Exception(f"Render error: {str(e)}")


    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _json_default(self, obj):
        """Fallback converter for objects that are not JSON-serialisable.

        Currently coerces QVariant and any other unsupported type to a string.
        This ensures the MCP reply can always be encoded to JSON instead of
        triggering a broken-pipe on the client side.
        """
        try:
            # Unwrap QVariant to its Python value when possible
            if isinstance(obj, QVariant):
                return obj if obj is None else obj.value() if hasattr(obj, "value") else str(obj)

            # Add more specific conversions as needed (e.g. QDate → ISO string)
            if hasattr(obj, "toString"):
                return obj.toString(_ISO_DATE)  # QDate / QDateTime / etc.

            # Fallback – stringify unknown objects
            return str(obj)
        except Exception:
            return str(obj)

    def _queue_response(self, cli, response):
        """Serialize and frame a response into a client's transmit queue."""
        try:
            payload = json.dumps(response, default=self._json_default).encode("utf-8")
        except Exception as e:
            payload = json.dumps({
                "status": "error",
                "message": f"Serialization error: {e}",
            }).encode("utf-8")

        if len(payload) > _MAX_MESSAGE_BYTES:
            payload = json.dumps({
                "status": "error",
                "message": f"Response exceeded the {_MAX_MESSAGE_BYTES // (1024 * 1024)} MB limit",
            }).encode("utf-8")
        state = self.clients.get(cli)
        if state is not None:
            state["tx"].extend(_FRAME_HEADER.pack(len(payload)))
            state["tx"].extend(payload)

    def _drop_client(self, cli):
        """Close and remove a client socket from the registry."""
        try:
            cli.close()
        except Exception:
            pass
        self.clients.pop(cli, None)
        self.client_count_changed.emit(len(self.clients))


class QgisMCPDockWidget(QDockWidget):
    """Dock widget for the WAI QGIS MCP plugin"""
    closed = pyqtSignal()
    server_status_changed = pyqtSignal(bool, int, int)
    
    def __init__(self, iface):
        super().__init__(_BRAND_NAME)
        self.iface = iface
        self.server = None
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)
        
        settings = QgsSettings()

        # Add port selection
        layout.addWidget(QLabel("Server Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(int(settings.value("qgis_mcp/port", 9876)))
        layout.addWidget(self.port_spin)

        # Add server control buttons
        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.start_server)
        layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop Server")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)

        self.autostart_check = QCheckBox("Start server automatically when QGIS opens")
        self.autostart_check.setChecked(settings.value("qgis_mcp/auto_start", False, type=bool))
        self.autostart_check.toggled.connect(
            lambda checked: QgsSettings().setValue("qgis_mcp/auto_start", checked)
        )
        layout.addWidget(self.autostart_check)

        self.diagnostics_check = QCheckBox("Log diagnostics to the QGIS Message Log")
        self.diagnostics_check.setChecked(
            settings.value("qgis_mcp/diagnostics", False, type=bool)
        )
        self.diagnostics_check.toggled.connect(self._set_diagnostics)
        layout.addWidget(self.diagnostics_check)

        # Add status label
        self.status_label = QLabel("Server: Stopped")
        layout.addWidget(self.status_label)
        
        # Add to dock widget
        self.setWidget(widget)
    
    def start_server(self):
        """Start the server"""
        if not self.server:
            port = self.port_spin.value()
            self.server = QgisMCPServer(port=port, iface=self.iface)
            self.server.client_count_changed.connect(self._update_client_count)
            
        if self.server.start():
            QgsSettings().setValue("qgis_mcp/port", self.server.port)
            self.status_label.setText(f"Server: Running on port {self.server.port}")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.port_spin.setEnabled(False)
            self.server_status_changed.emit(True, self.server.port, 0)
        else:
            self.status_label.setText(
                f"Server: Failed to start on port {self.server.port}: {self.server.last_error}"
            )
            self.iface.messageBar().pushCritical(
                _BRAND_NAME,
                f"Could not start on port {self.server.port}: {self.server.last_error}",
            )

    def _set_diagnostics(self, enabled):
        """Persist diagnostic logging without recording command payloads."""
        QgsSettings().setValue("qgis_mcp/diagnostics", enabled)
        if enabled:
            _dbg("Diagnostics enabled")
    
    def stop_server(self):
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None
            
        self.status_label.setText("Server: Stopped")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.port_spin.setEnabled(True)
        self.server_status_changed.emit(False, self.port_spin.value(), 0)

    def _update_client_count(self, count):
        """Show whether an MCP client is actually connected."""
        if not self.server or not self.server.running:
            return
        noun = "client" if count == 1 else "clients"
        self.status_label.setText(
            f"Server: Running on port {self.server.port} — {count} {noun} connected"
        )
        self.server_status_changed.emit(True, self.server.port, count)
        
    def closeEvent(self, event):
        """Stop server on dock close"""
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)


class QgisMCPPlugin:
    """Main plugin class for WAI QGIS MCP"""
    
    def __init__(self, iface):
        self.iface = iface
        self.dock_widget = None
        self.action = None
    
    def initGui(self):
        """Initialize GUI"""
        # Create action
        self.action = QAction(
            _BRAND_NAME,
            self.iface.mainWindow()
        )
        self.action.setCheckable(True)
        self.action.setToolTip(f"{_BRAND_NAME} server stopped")
        self.action.triggered.connect(self.toggle_dock)
        
        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu(_BRAND_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

        # Auto-start the server if the user enabled it in the dock widget.
        if QgsSettings().value("qgis_mcp/auto_start", False, type=bool):
            self._ensure_dock()
            self.dock_widget.hide()
            self.dock_widget.start_server()

        QTimer.singleShot(750, self._show_first_run_help)

    def _show_first_run_help(self):
        """Give a newly installed user an in-QGIS path to a working setup."""
        settings = QgsSettings()
        if settings.value("qgis_mcp/first_run_complete", False, type=bool):
            return
        settings.setValue("qgis_mcp/first_run_complete", True)
        QMessageBox.information(
            self.iface.mainWindow(),
            f"{_BRAND_NAME} installed",
            f"{_BRAND_NAME} is ready.\n\n"
            f"1. Open Plugins -> {_BRAND_NAME} -> {_BRAND_NAME}.\n"
            "2. Click Start Server.\n"
            "3. Enable automatic startup if you want QGIS to reconnect next time.\n"
            "4. Configure your MCP client using the project README.\n\n"
            "The status panel shows when an MCP client is actually connected.",
        )

    def _ensure_dock(self):
        """Create the dock widget if it doesn't exist yet."""
        if not self.dock_widget:
            self.dock_widget = QgisMCPDockWidget(self.iface)
            self.iface.addDockWidget(_RIGHT_DOCK_AREA, self.dock_widget)
            self.dock_widget.closed.connect(self.dock_closed)
            self.dock_widget.server_status_changed.connect(self._update_action_status)

    def _update_action_status(self, running, port, client_count):
        """Reflect server and connection state in the persistent toolbar action."""
        if running:
            noun = "client" if client_count == 1 else "clients"
            self.action.setToolTip(
                f"{_BRAND_NAME} running on port {port} — {client_count} {noun} connected"
            )
        else:
            self.action.setToolTip(f"{_BRAND_NAME} server stopped")

    def toggle_dock(self, checked):
        """Toggle the dock widget"""
        if checked:
            self._ensure_dock()
            self.dock_widget.show()
        else:
            # Hide dock widget
            if self.dock_widget:
                self.dock_widget.hide()
    
    def dock_closed(self):
        """Handle dock widget closed"""
        self.action.setChecked(False)
    
    def unload(self):
        """Unload plugin"""
        # Stop server if running
        if self.dock_widget:
            self.dock_widget.stop_server()
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None
            
        # Remove plugin menu item and toolbar icon
        self.iface.removePluginMenu(_BRAND_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)


# Plugin entry point
def classFactory(iface):
    return QgisMCPPlugin(iface)
