#!/usr/bin/env python3
"""
QGIS MCP Client - Simple client to connect to the QGIS MCP server
"""

import logging
from contextlib import asynccontextmanager
import socket
import json
from typing import AsyncIterator, Dict, Any
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QgisMCPServer")

class QgisMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.socket = None
    
    def connect(self):
        """Connect to the QGIS MCP server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"Error connecting to server: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from the server"""
        if self.socket:
            self.socket.close()
            self.socket = None
    
    def send_command(self, command_type, params=None):
        """Send a command to the server and get the response"""
        if not self.socket:
            print("Not connected to server")
            return None
        
        # Create command
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        try:
            # Send the command
            self.socket.sendall(json.dumps(command).encode('utf-8'))
            
            # Receive the response
            response_data = b''
            while True:
                chunk = self.socket.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                
                # Try to decode as JSON to see if it's complete
                try:
                    json.loads(response_data.decode('utf-8'))
                    break  # Valid JSON, we have the full message
                except json.JSONDecodeError:
                    continue  # Keep receiving
            
            # Parse and return the response
            return json.loads(response_data.decode('utf-8'))
            
        except Exception as e:
            print(f"Error sending command: {str(e)}")
            return None

_qgis_connection = None

def get_qgis_connection():
    """Get or create a persistent Qgis connection"""
    global _qgis_connection
    
    # If we have an existing connection, check if it's still valid
    if _qgis_connection is not None:
        # Test if the connection is still alive with a simple ping
        try:
            # Just try to send a small message to check if the socket is still connected
            _qgis_connection.sock.sendall(b'')
            return _qgis_connection
        except Exception as e:
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _qgis_connection.disconnect()
            except Exception:
                pass
            _qgis_connection = None
    
    # Create a new connection if needed
    if _qgis_connection is None:
        _qgis_connection = QgisMCPServer(host="localhost", port=9876)
        if not _qgis_connection.connect():
            logger.error("Failed to connect to Qgis")
            _qgis_connection = None
            raise Exception("Could not connect to Qgis. Make sure the Qgis plugin is running.")
        logger.info("Created new persistent connection to Qgis")
    
    return _qgis_connection

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools
    
    try:
        # Just log that we're starting up
        logger.info("QgisMCPServer server starting up")
        
        # Try to connect to Qgis on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            qgis = get_qgis_connection()
            logger.info("Successfully connected to Qgis on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Qgis on startup: {str(e)}")
            logger.warning("Make sure the Qgis addon is running before using Qgis resources or tools")
        
        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _qgis_connection
        if _qgis_connection:
            logger.info("Disconnecting from Qgis on shutdown")
            _qgis_connection.disconnect()
            _qgis_connection = None
        logger.info("QgisMCPServer server shut down")

mcp = FastMCP(
    "Qgis_mcp",
    description="Qgis integration through the Model Context Protocol",
    lifespan=server_lifespan
)

@mcp.tool()
def ping(ctx: Context) -> str:
    """
    Ping the QGIS MCP plugin to verify that the python client can reach the
    running QGIS instance.

    Returns
    -------
    str
        JSON string such as:
            {
              "status": "success",
              "result": {"pong": true}
            }
        No side-effects.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("ping")
    return json.dumps(result, indent=2)

@mcp.tool()
def get_qgis_info(ctx: Context) -> str:
    """
    Retrieve basic information about the active QGIS application.

    Returns
    -------
    str
        JSON string with keys:
          • qgis_version   – full version string (e.g. "3.36.0-Lima")
          • profile_folder – absolute path to the user profile directory
          • plugins_count  – number of loaded plugins
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_qgis_info")
    return json.dumps(result, indent=2)

@mcp.tool()
def load_project(ctx: Context, path: str) -> str:
    """
    Load an existing QGIS project (.qgs or .qgz) from disk.

    Parameters
    ----------
    path : str
        Absolute path to the project file that QGIS can open.

    Returns
    -------
    str
        JSON string {"status":"success","result":{"loaded": <path>,
        "layer_count": <int>}}

    Side Effects
    ------------
    Replaces the currently opened project in QGIS and refreshes the map view.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("load_project", {"path": path})
    return json.dumps(result, indent=2)

@mcp.tool()
def create_new_project(ctx: Context, path: str) -> str:
    """
    Create a brand-new, empty QGIS project and immediately save it to *path*.

    Parameters
    ----------
    path : str
        Destination path ending in .qgz or .qgs. Parent directories must
        already exist.

    Returns
    -------
    str
        JSON confirmation message with layer_count (normally 0).

    Side Effects
    ------------
    Clears any project that is currently open in QGIS.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("create_new_project", {"path": path})
    return json.dumps(result, indent=2)

@mcp.tool()
def get_project_info(ctx: Context) -> str:
    """
    Return a concise summary of the currently open project.

    Returns
    -------
    str
        JSON string containing:
          • filename
          • title
          • layer_count
          • crs (auth id)
          • layers – up to ten layer descriptors {id,name,type,visible}
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_project_info")
    return json.dumps(result, indent=2)

@mcp.tool()
def add_vector_layer(ctx: Context, path: str, provider: str = "ogr", name: str = None) -> str:
    """
    Add a vector dataset (Shapefile, GeoJSON, GeoPackage, …) to the project.

    Parameters
    ----------
    path : str
        File path or data-source URI recognised by QGIS.
    provider : str, optional
        QGIS data provider key, default "ogr".
    name : str, optional
        Custom display name for the layer (defaults to the file base-name).

    Returns
    -------
    str
        JSON description of the new layer {id,name,type,feature_count}.

    Side Effects
    ------------
    Inserts the layer into the current project and triggers a map refresh.
    """
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_vector_layer", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def add_raster_layer(ctx: Context, path: str, provider: str = "gdal", name: str = None) -> str:
    """
    Add a raster dataset (e.g. GeoTIFF, JPEG2000) to the project.

    Parameters
    ----------
    path : str
        Absolute file path or URI to the raster.
    provider : str, optional
        Data provider key, default "gdal".
    name : str, optional
        Layer name to display in the layer panel.

    Returns
    -------
    str
        JSON string with {id,name,type,width,height}.

    Side Effects
    ------------
    Adds the raster layer to the project.
    """
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_raster_layer", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def get_layers(ctx: Context) -> str:
    """
    List every layer currently loaded in the project.

    Returns
    -------
    str
        JSON string containing an array of layer descriptors. Each descriptor
        includes id, name, type, visible, and type-specific metadata.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layers")
    return json.dumps(result, indent=2)

@mcp.tool()
def remove_layer(ctx: Context, layer_id: str) -> str:
    """
    Remove a layer from the project.

    Parameters
    ----------
    layer_id : str
        The internal QGIS layer id obtained from other tools.

    Returns
    -------
    str
        JSON {"status":"success","result":{"removed": layer_id}}

    Side Effects
    ------------
    Deletes the layer from the project and the layer tree.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("remove_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)

@mcp.tool()
def zoom_to_layer(ctx: Context, layer_id: str) -> str:
    """
    Zoom the map canvas to the full extent of a specific layer.

    Parameters
    ----------
    layer_id : str
        QGIS layer id.

    Returns
    -------
    str
        JSON {"status":"success","result":{"zoomed_to": layer_id}}

    Side Effects
    ------------
    Changes the visible map extent in the QGIS UI only.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("zoom_to_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)

@mcp.tool()
def get_layer_features(ctx: Context, layer_id: str, limit: int = 10) -> str:
    """
    Retrieve attribute and geometry data for a subset of features from a
    vector layer.

    Parameters
    ----------
    layer_id : str
        Target vector layer id.
    limit : int, optional
        Maximum number of features to return (default 10).

    Returns
    -------
    str
        JSON with layer_id, feature_count, fields and a *features* array where
        each element is {id, attributes, geometry} (geometry expressed as WKT).
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layer_features", {"layer_id": layer_id, "limit": limit})
    return json.dumps(result, indent=2)

@mcp.tool()
def execute_processing(ctx: Context, algorithm: str, parameters: dict) -> str:
    """
    Run a QGIS Processing algorithm.

    Parameters
    ----------
    algorithm : str
        Provider-qualified algorithm id (e.g. "native:buffer").
    parameters : dict
        Dictionary of parameter names and values exactly as expected by the
        algorithm.

    Returns
    -------
    str
        JSON {"status":"success","result":{"algorithm": id, "result": {...}}}
        All complex objects (layers, paths) are serialised to strings.

    Side Effects
    ------------
    Depends on the algorithm – may create layers, files or modify data.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_processing", {"algorithm": algorithm, "parameters": parameters})
    return json.dumps(result, indent=2)

@mcp.tool()
def save_project(ctx: Context, path: str = None) -> str:
    """
    Save the current project.

    Parameters
    ----------
    path : str, optional
        Destination .qgs/.qgz file. If omitted the project is saved over the
        existing file on disk.

    Returns
    -------
    str
        JSON confirmation {"status":"success","result":{"saved": path}}

    Side Effects
    ------------
    Writes a project file to disk.
    """
    qgis = get_qgis_connection()
    params = {}
    if path:
        params["path"] = path
    result = qgis.send_command("save_project", params)
    return json.dumps(result, indent=2)

@mcp.tool()
def render_map(ctx: Context, path: str, width: int = 800, height: int = 600) -> str:
    """
    Export the current map view to an image on disk.

    Parameters
    ----------
    path : str
        Output image path (format inferred from extension).
    width : int, optional
        Image width in pixels, default 800.
    height : int, optional
        Image height in pixels, default 600.

    Returns
    -------
    str
        JSON {rendered: true, path, width, height}

    Side Effects
    ------------
    Renders the map and writes an image file; does not alter project data.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("render_map", {"path": path, "width": width, "height": height})
    return json.dumps(result, indent=2)

@mcp.tool()
def execute_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code inside the QGIS Python interpreter.

    WARNING: This is a powerful tool that can be used when none of the
    other tools are sufficient or would be too inefficient.

    Parameters
    ----------
    code : str
        Python source code string. The execution namespace already contains:
          • qgis (qgis.core.Qgis)
          • QgsProject
          • iface (Qgis interface)
          • QgsApplication
          • QgsVectorLayer, QgsRasterLayer
          • QgsCoordinateReferenceSystem

    Returns
    -------
    str
        JSON {"status":"success","result":{"executed": true}}

    Side Effects
    ------------
    Whatever the supplied code performs – it can create, modify or delete
    layers and data.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code})
    return json.dumps(result, indent=2)

def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()
