def test_mcp_server_imports_with_installed_sdk():
    from facelink.mcp_server import mcp

    assert mcp is not None
    assert mcp.name == "FaceLink"
