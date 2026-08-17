# Local bridge protocol 1.0

The Blender extension writes one JSON discovery record per running Blender process. The
directory is `${FACELINK_INSTANCE_DIR}` when configured, otherwise
`${TEMP}/facelink/instances`.

All requests require `Authorization: Bearer <token>` from that record.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health` | Static protocol and capability information |
| `POST` | `/v1/jobs` | Queue `scan_scene`, `apply_patch` or `undo` |
| `GET` | `/v1/jobs/{id}` | Poll a job until succeeded or failed |

Scene access is asynchronous because Blender's Python API is not thread-safe. HTTP handler
threads only enqueue data; a registered `bpy.app.timer` performs every scene read or write.

