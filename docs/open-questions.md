# Open questions (before any Pi test)

1. **Read-only Files Disk.** The builder only reads `/mnt/idea-files`, but the bind mode is set
   by the Engine's `filesMount` override (rw for Nextcloud). A per-app `readOnly: true` option
   in `x-app.filesMount` would enforce it.
2. **Nextcloud URL.** Compose has no variable for Nextcloud's port on the same Pi;
   `NEXTCLOUD_URL` is empty by default (Files Disk only). Engine could inject it.
3. **Who may build without Nextcloud?** With `NEXTCLOUD_URL` empty, anyone on the LAN who can
   reach the port can build (no login). OK for v1?
4. **Icon** is a placeholder (Kolibri's).
5. **Kolibri 0.15.5** in app-kolibri is old; the design note assumed 0.19.5. Upgrading Kolibri
   means bumping `requirements.txt`/Dockerfile here in lockstep (import test guards it).
6. **Image resources** are HTML5-zip wrapped; zip rendering in Kolibri's UI was not checked in a
   browser in the spike (resource imports and is `available`).
