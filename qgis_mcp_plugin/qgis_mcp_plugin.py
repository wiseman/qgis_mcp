import os
import ast
import contextlib
import io
import json
import math
import socket
import traceback
import sys
from qgis.core import *
from qgis.gui import *
# Bind the package itself so snippets can reach qgis.core/qgis.utils. Must come
# after the star imports, which would otherwise shadow the name.
import qgis
import qgis.core
import qgis.gui
from qgis.PyQt.QtCore import QObject, pyqtSignal, QTimer, Qt, QSize, QVariant
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QVBoxLayout, QLabel, QPushButton, QSpinBox, QWidget
from qgis.PyQt.QtGui import QIcon, QColor
from datetime import datetime

# Filename used when compiling user snippets, so tracebacks name the source.
_EXEC_FILENAME = "<qgis_mcp>"

# Cap on any single string field sent back to the client. A print() inside a
# loop over a large layer would otherwise flood the caller's context.
_MAX_OUTPUT_CHARS = 20_000

# Sentinel: the snippet ended in a statement, so there is no value at all
# (distinct from a final expression that evaluated to None).
_NOTHING = object()


def _truncate(text, limit=_MAX_OUTPUT_CHARS):
    """Clip *text*, marking the clip so the reader knows it is a prefix."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


# Simple helper to write debug lines to stderr so they appear in QGIS console
def _dbg(msg: str):
    pass
    # Write message to /Users/wisej041/qgis_mcp.log
    # with open("/Users/wisej041/qgis_mcp.log", "a") as f:
    #     f.write(f"{msg}\n")

class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""
    
    def __init__(self, host='localhost', port=9876, iface=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.running = False
        self.socket = None
        # Maintain multiple client sockets -> receive buffers
        self.clients = {}  # dict mapping socket objects to bytes buffers
        # Deprecated single-client attributes retained for backward compatibility
        self.client = None  # kept but unused after multi-client refactor
        self.timer = None
    
    def start(self):
        """Start the server"""
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
            self.timer.start(100)  # 100ms interval
            
            _dbg("QGIS MCP server started and listening")
            return True
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to start server: {str(e)}", "QGIS MCP", Qgis.Critical)
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
        self.client = None
        _dbg("QGIS MCP server stopped")
    
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
                self.clients[client] = b''
                _dbg(f"Accepted connection from {address}; active clients = {len(self.clients)}")
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
                data = cli.recv(8192)
                if data:
                    self.clients[cli] += data
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

            # Try to parse a full JSON object from the buffer
            try:
                buffer_str = self.clients[cli].decode('utf-8')
                command = json.loads(buffer_str)
                # Clear buffer only after successful parse
                self.clients[cli] = b''
            except json.JSONDecodeError:
                # Incomplete transmission; wait for more bytes
                continue

            # Execute the requested command
            response = self.execute_command(command)

            # Serialize response, always returning JSON even on failures
            try:
                response_json = json.dumps(response, default=self._json_default)
            except Exception as e:
                error_payload = {
                    "status": "error",
                    "message": f"Serialization error: {str(e)}"
                }
                response_json = json.dumps(error_payload)

            # Send back to the originating client
            try:
                cli.sendall(response_json.encode('utf-8'))
            except Exception as e:
                _dbg(f"Error sending response: {str(e)} – dropping client")
                self._drop_client(cli)

    def execute_command(self, command):
        """Execute a command"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            _dbg(f"execute_command: type={cmd_type} params={params}")
            
            # "ping" is kept for socket-level liveness checks even though the
            # MCP server exposes no ping tool; everything else project- or
            # layer-related happens through execute_code.
            handlers = {
                "ping": self.ping,
                "execute_code": self.execute_code,
                "get_layers": self.get_layers,
                "get_layer_features": self.get_layer_features,
                "render_map": self.render_map,
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    _dbg(f"Executing handler for {cmd_type}")
                    result = handler(**params)
                    _dbg("Handler execution complete")
                    return {"status": "success", "result": result}
                except Exception as e:
                    _dbg(f"Error in handler: {str(e)}")
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
                
        except Exception as e:
            _dbg(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}
    
    # Command handlers
    def ping(self, **kwargs):
        """Simple ping command"""
        return {"pong": True}
    
    def _get_layer_type(self, layer):
        """Helper to get layer type as string"""
        if layer.type() == QgsMapLayer.VectorLayer:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == QgsMapLayer.RasterLayer:
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
        """Execute arbitrary PyQGIS code, notebook-style.

        The snippet is executed as-is.  If its final statement is a bare
        expression, that expression is evaluated and its value becomes the
        ``result`` — the same semantics as a Jupyter cell::

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
            if layer.type() == QgsMapLayer.VectorLayer:
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
            if layer.type() == QgsMapLayer.VectorLayer:
                layer_info.update({
                    "feature_count": layer.featureCount(),
                    "geometry_type": layer.geometryType()
                })
            elif layer.type() == QgsMapLayer.RasterLayer:
                layer_info.update({
                    "width": layer.width(),
                    "height": layer.height()
                })
                
            layers.append(layer_info)
        
        return layers
    
    def get_layer_features(self, layer_id, limit=10, **kwargs):
        """Get features from a vector layer"""
        project = QgsProject.instance()
        _dbg(f"get_layer_features: layer_id={layer_id}, limit={limit}")
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            
            if layer.type() != QgsMapLayer.VectorLayer:
                raise Exception(f"Layer is not a vector layer: {layer_id}")
            _dbg(f"got layer: {layer}")
            features = []
            for i, feature in enumerate(layer.getFeatures()):
                if i >= limit:
                    break
                _dbg(f"* got feature {i}: {feature}")
                # Extract attributes
                attrs = {}
                for field in layer.fields():
                    attrs[str(field.name())] = str(feature.attribute(field.name()))
                _dbg(f"* got attrs: {attrs}")
                # Extract geometry – full geometry for points, else centroid & bbox
                geom = None
                if feature.hasGeometry():
                    geom_obj = feature.geometry()
                    try:
                        # If the geometry is a point (or multipoint with exactly one point), return full WKT
                        if geom_obj.type() == QgsWkbTypes.PointGeometry:
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
                _dbg(f"* got geom: {geom}")
                features.append({
                    "id": feature.id(),
                    "attributes": attrs,
                    "geometry": geom
                })
            
            _dbg("returning features")
            return {
                "layer_id": layer_id,
                "feature_count": layer.featureCount(),
                "features": features,
                "fields": [str(field.name()) for field in layer.fields()]
            }
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
    def render_map(self, path, width=800, height=600, include_hidden=False, transparent=False, **kwargs):
        """
        Render the current map view to an image, respecting layer-tree order & visibility.
        """
        try:
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
            ms.setFlag(QgsMapSettings.Antialiasing, True)
            ms.setFlag(QgsMapSettings.DrawLabeling, True)  # labels, if any
            ms.setFlag(QgsMapSettings.UseAdvancedEffects, True)  # blend modes, layer opacity, etc.

            # --- 3) Render ---
            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()

            img = job.renderedImage()
            if not img.save(path):
                raise Exception(f"Failed to save rendered image to {path}")

            return {"rendered": True, "path": path, "width": width, "height": height,
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
                return obj.toString(Qt.ISODate)  # QDate / QDateTime / etc.

            # Fallback – stringify unknown objects
            return str(obj)
        except Exception:
            return str(obj)

    def _drop_client(self, cli):
        """Close and remove a client socket from the registry."""
        try:
            cli.close()
        except Exception:
            pass
        self.clients.pop(cli, None)


class QgisMCPDockWidget(QDockWidget):
    """Dock widget for the QGIS MCP plugin"""
    closed = pyqtSignal()
    
    def __init__(self, iface):
        super().__init__("QGIS MCP")
        self.iface = iface
        self.server = None
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)
        
        # Add port selection
        layout.addWidget(QLabel("Server Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(9876)
        layout.addWidget(self.port_spin)
        
        # Add server control buttons
        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.start_server)
        layout.addWidget(self.start_button)
        
        self.stop_button = QPushButton("Stop Server")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)
        
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
            
        if self.server.start():
            self.status_label.setText(f"Server: Running on port {self.server.port}")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.port_spin.setEnabled(False)
    
    def stop_server(self):
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None
            
        self.status_label.setText("Server: Stopped")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.port_spin.setEnabled(True)
        
    def closeEvent(self, event):
        """Stop server on dock close"""
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)


class QgisMCPPlugin:
    """Main plugin class for QGIS MCP"""
    
    def __init__(self, iface):
        self.iface = iface
        self.dock_widget = None
        self.action = None
    
    def initGui(self):
        """Initialize GUI"""
        # Create action
        self.action = QAction(
            "QGIS MCP",
            self.iface.mainWindow()
        )
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)
        
        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addToolBarIcon(self.action)
    
    def toggle_dock(self, checked):
        """Toggle the dock widget"""
        if checked:
            # Create dock widget if it doesn't exist
            if not self.dock_widget:
                self.dock_widget = QgisMCPDockWidget(self.iface)
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
                # Connect close event
                self.dock_widget.closed.connect(self.dock_closed)
            else:
                # Show existing dock widget
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
        self.iface.removePluginMenu("QGIS MCP", self.action)
        self.iface.removeToolBarIcon(self.action)


# Plugin entry point
def classFactory(iface):
    return QgisMCPPlugin(iface)
