# Remote access

The MVP dashboard binds to the host you choose. For a trusted home/LAN setup:

```bash
awb serve --host 0.0.0.0 --port 8000
```

For access away from home, do **not** expose port 8000 directly to the public Internet. A practical zero-cost personal setup is to install a private mesh VPN such as Tailscale on the host PC and on the iPad, then open the host's private tailnet address. Authentication/TLS inside the application itself remains a roadmap item.

The PC must remain powered on and the AWB service must be running. Docker Compose uses `restart: unless-stopped`, which is the preferred always-available local deployment.
