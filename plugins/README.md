# Calibre µMCP Bridge plugin

Build locally:

```sh
sh plugins/build-plugin.sh
```

This produces `plugins/calibre-umcp-plugin.zip`.

## Install in a Calibre profile

Inside the Calibre container/profile, install with:

```sh
calibre-customize -a /path/to/calibre-umcp-plugin.zip
```


- container: `calibre`
- image: `linuxserver/calibre:latest`
- profile/config mount: `/config`
- plugin directory: `/config/.config/calibre/plugins`
- library mount: `/books`

After installing, restart Calibre or reload the GUI. The plugin action appears as `µMCP Bridge` and exposes menu items to start, inspect, or stop the bridge.


Useful environment variables:

- `CALIBRE_UMCP_PORT` — bridge port, default `9000`.
- `CALIBRE_UMCP_BRIDGE_HOST` — bind host, default `127.0.0.1`.
- `CALIBRE_UMCP_BRIDGE_TOKEN` — optional bearer token for `/rpc`.
- `CALIBRE_UMCP_AUDIT_PATH` — optional JSONL file for bridge audit records.

## Safety status

Read-only bridge operations are implemented. Mutating operations currently fail closed and create audit/status records until each operation is mapped to Calibre's in-process job APIs.
