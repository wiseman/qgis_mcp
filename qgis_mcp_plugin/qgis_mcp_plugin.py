import os
import json
import socket
import traceback
import sys
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import QObject, pyqtSignal, QTimer, Qt, QSize, QVariant
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QVBoxLayout, QLabel, QPushButton, QSpinBox, QWidget
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.utils import active_plugins
from datetime import datetime

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
            
            handlers = {
                "ping": self.ping,
                "get_qgis_info": self.get_qgis_info,
                "load_project": self.load_project,
                "get_project_info": self.get_project_info,
                "execute_code": self.execute_code,
                "add_vector_layer": self.add_vector_layer,
                "add_raster_layer": self.add_raster_layer,
                "get_layers": self.get_layers,
                "remove_layer": self.remove_layer,
                "zoom_to_layer": self.zoom_to_layer,
                "get_layer_features": self.get_layer_features,
                "execute_processing": self.execute_processing,
                "save_project": self.save_project,
                "render_map": self.render_map,
                "create_new_project": self.create_new_project,
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
    
    def get_qgis_info(self, **kwargs):
        """Get basic QGIS information"""
        return {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins)
        }
    
    def get_project_info(self, **kwargs):
        """Get information about the current QGIS project"""
        project = QgsProject.instance()
        
        # Get basic project information
        info = {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "layers": []
        }
        
        # Add basic layer information (limit to 10 layers for performance)
        layers = list(project.mapLayers().values())
        for i, layer in enumerate(layers):
            if i >= 10:  # Limit to 10 layers
                break
                
            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": layer.isValid() and project.layerTreeRoot().findLayer(layer.id()).isVisible()
            }
            info["layers"].append(layer_info)
        
        return info
    
    def _get_layer_type(self, layer):
        """Helper to get layer type as string"""
        if layer.type() == QgsMapLayer.VectorLayer:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == QgsMapLayer.RasterLayer:
            return "raster"
        else:
            return str(layer.type())
    
    def execute_code(self, code, **kwargs):
        """Execute arbitrary PyQGIS code and capture its return value.

        The provided *code* string is wrapped inside a temporary function so
        that its ``return`` statement becomes the value that is sent back to
        the MCP client.  Example snippet received from the client::

            layer = QgsProject.instance().mapLayersByName("roads")[0]
            return layer.featureCount()

        Whatever is returned by the snippet will be JSON-encoded and returned
        to the client.

        If the value cannot be JSON-serialised the reply will be::

            {"error": "Non-JSON-serialisable result: …"}
        """
        try:
            # Prepare execution namespace with common PyQGIS symbols
            namespace = {
                "qgis": Qgis,
                "QgsProject": QgsProject,
                "iface": self.iface,
                "QgsApplication": QgsApplication,
                "QgsVectorLayer": QgsVectorLayer,
                "QgsRasterLayer": QgsRasterLayer,
                "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
            }

            # Wrap the incoming snippet in a function so it can use `return`
            func_name = "_mcp_user_code"
            wrapped_lines = [f"def {func_name}():"]
            for line in code.splitlines():
                wrapped_lines.append("    " + line)
            wrapped_code = "\n".join(wrapped_lines)

            # Compile and execute the wrapper, then call the generated func
            exec(wrapped_code, namespace)
            result = namespace[func_name]()

            # Ensure the result can be JSON-serialised; if not, return an error
            try:
                json.dumps(result)
                return {"return": result}
            except TypeError:
                return {"error": f"Non-JSON-serialisable result: {str(result)}"}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")
    
    def add_vector_layer(self, path, name=None, provider="ogr", **kwargs):
        """Add a vector layer to the project"""
        if not name:
            name = os.path.basename(path)
            
        # Create the layer
        layer = QgsVectorLayer(path, name, provider)
        
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")
        
        # Add to project
        QgsProject.instance().addMapLayer(layer)
        
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount()
        }
    
    def add_raster_layer(self, path, name=None, provider="gdal", **kwargs):
        """Add a raster layer to the project"""
        if not name:
            name = os.path.basename(path)
            
        # Create the layer
        layer = QgsRasterLayer(path, name, provider)
        
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")
        
        # Add to project
        QgsProject.instance().addMapLayer(layer)
        
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": "raster",
            "width": layer.width(),
            "height": layer.height()
        }
    
    def get_layers(self, **kwargs):
        """Get all layers in the project"""
        project = QgsProject.instance()
        layers = []
        
        for layer_id, layer in project.mapLayers().items():
            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": project.layerTreeRoot().findLayer(layer_id).isVisible()
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
    
    def remove_layer(self, layer_id, **kwargs):
        """Remove a layer from the project"""
        project = QgsProject.instance()
        
        if layer_id in project.mapLayers():
            project.removeMapLayer(layer_id)
            return {"removed": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
    def zoom_to_layer(self, layer_id, **kwargs):
        """Zoom to a layer's extent"""
        project = QgsProject.instance()
        
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
            return {"zoomed_to": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
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
                # Extract geometry if available
                geom = None
                if feature.hasGeometry():
                    geom = {
                        "type": str(feature.geometry().type()),
                        "wkt": str(feature.geometry().asWkt(precision=4))
                    }
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
    
    def execute_processing(self, algorithm, parameters, **kwargs):
        """Execute a processing algorithm"""
        try:
            import processing
            result = processing.run(algorithm, parameters)
            return {
                "algorithm": algorithm,
                "result": {k: str(v) for k, v in result.items()}  # Convert values to strings for JSON
            }
        except Exception as e:
            raise Exception(f"Processing error: {str(e)}")
    
    def save_project(self, path=None, **kwargs):
        """Save the current project"""
        project = QgsProject.instance()
        
        if not path and not project.fileName():
            raise Exception("No project path specified and no current project path")
        
        save_path = path if path else project.fileName()
        if project.write(save_path):
            return {"saved": save_path}
        else:
            raise Exception(f"Failed to save project to {save_path}")
    
    def load_project(self, path, **kwargs):
        """Load a project"""
        project = QgsProject.instance()
        
        if project.read(path):
            self.iface.mapCanvas().refresh()
            return {
                "loaded": path,
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to load project from {path}")
    
    def create_new_project(self, path, **kwargs):
        """
        Creates a new QGIS project and saves it at the specified path.
        If a project is already loaded, it clears it before creating the new one.
        
        :param project_path: Full path where the project will be saved
                            (e.g., 'C:/path/to/project.qgz')
        """
        project = QgsProject.instance()
        
        if project.fileName():
            project.clear()
        
        project.setFileName(path)
        self.iface.mapCanvas().refresh()
        
        # Save the project
        if project.write():
            return {
                "created": f"Project created and saved successfully at: {path}",
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to save project to {path}")
    
    def render_map(self, path, width=800, height=600, **kwargs):
        """Render the current map view to an image"""
        try:
            # Create map settings
            ms = QgsMapSettings()
            
            # Set layers to render
            layers = list(QgsProject.instance().mapLayers().values())
            ms.setLayers(layers)
            
            # Set map canvas properties
            rect = self.iface.mapCanvas().extent()
            ms.setExtent(rect)
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)
            
            # Create the render
            render = QgsMapRendererParallelJob(ms)
            
            # Start rendering
            render.start()
            render.waitForFinished()
            
            # Get the image and save
            img = render.renderedImage()
            if img.save(path):
                return {
                    "rendered": True,
                    "path": path,
                    "width": width,
                    "height": height
                }
            else:
                raise Exception(f"Failed to save rendered image to {path}")
                
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
