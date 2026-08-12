# Minecraft server setup

Portable configuration examples and shared management scripts for two mutually exclusive servers on TCP port 25565:

- Fabric 26.2 Hardcore with Distant Horizons (Java 25, 2–6 GB RAM)
- RLCraft Dregora 1.12.2 with Forge 14.23.5.2860 (Java 8, 6–8 GB RAM)

## Important: saves are not stored in Git

Live worlds, player data, logs, Java runtimes, server/mod JARs, and files containing secrets or machine-specific paths are intentionally ignored. The Fabric Distant Horizons database is larger than GitHub's per-file limit, and Git is unsafe for synchronizing a running Minecraft world.

To move all progress between machines:

1. Run `save-all flush`, followed by `stop`.
2. Wait for the Java process to exit.
3. Archive the complete `Minecraft Server` directory, including ignored files.
4. Transfer and extract the archive on the destination machine.
5. Never run divergent copies and attempt to merge their worlds.

On an installed server, both server payloads are isolated below the ignored `profiles/` directory:

- `profiles/fabric-26-2-hardcore`
- `profiles/rlcraft-dregora`

The private repository intentionally does not contain either profile payload. Its root contains only shared Windows profile-manager code, convenience launchers, and the portable `server-profiles.example.json`. Native macOS launch scripts still need to be added before the management interface is cross-platform.
