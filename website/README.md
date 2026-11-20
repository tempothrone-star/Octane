# Octane project website

A static overview, rendered project handbook, searchable source browser and source download served by Nginx. No OpenClaw execution API or project state is published.

## Build

```sh
python3 -m pip install -r website/requirements.txt
python3 website/build.py --output "$HOME/.local/share/octane-site/public"
```

Documentation and source come from the committed public Git tree (`HEAD`), not uncommitted checkout contents. Website templates/assets come from `website/`. Commit documentation changes before rebuilding. Symlinks, binaries, oversized files, credential filenames and database files are excluded. The downloadable archive uses the same filtered public files.

## Serve with Nginx and Cloudflare

`nginx.conf.template` is a complete unprivileged Nginx configuration. Replace `@STATE@` with an absolute writable directory for the PID/logs and `@PUBLIC@` with the generated site's absolute directory. It listens on `127.0.0.1:8080`, leaving the system Nginx on port 80 in place. Validate the rendered config with `nginx -t -c /absolute/path/nginx.conf` before starting it.

```sh
nginx -c /absolute/path/nginx.conf
cloudflared tunnel --url http://127.0.0.1:8080
```

Cloudflare prints a free public HTTPS URL. A Quick Tunnel requires no account/domain, but has no uptime guarantee and receives a new hostname when recreated. The computer must stay awake and online. For a stable custom domain, use a named Cloudflare Tunnel. See [Cloudflare's Quick Tunnel documentation](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

On the configured development computer, the two services are managed with:

```sh
systemctl --user status octane-site-nginx octane-site-tunnel
journalctl --user -u octane-site-tunnel -n 40 --no-pager
systemctl --user stop octane-site-tunnel octane-site-nginx
systemctl --user start octane-site-nginx octane-site-tunnel
```

The generated site and Nginx config live under `~/.local/share/octane-site/`; user service units live under `~/.config/systemd/user/`. Starting the tunnel again changes the public URL. User services are enabled for future logins; availability after logout depends on the system's user-session configuration.
