# Running it on TrueNAS

The stack lives in `/mnt/AppPool/DockerStacks/episode-sorter` and runs next to the other
apps on the box.

| | |
| --- | --- |
| Dashboard, local | `http://<nas-ip>:18080` |
| Dashboard, remote | `https://<hostname>`, behind Cloudflare Access |
| JDownloader, local | `http://<nas-ip>:5800` |
| JDownloader, remote | `https://<hostname>`, behind Cloudflare Access, plus `my.jdownloader.org` |
| Download folder | `/mnt/SmallPool/dataGrepDataset/Downloads/JDownloader` |
| Container user | `3000:0` (smb), so moved files stay usable over SMB |

The Cloudflare tunnel runs as its own TrueNAS app, so the `cloudflared` service in the
compose file sits behind the `tunnel` profile and does not start here.

One mount per pool, not one per folder. A rename only works inside a single mount point,
separate mounts would turn every move within a pool into a full copy.

```bash
ssh nas
cd /mnt/AppPool/DockerStacks/episode-sorter
git pull && sudo docker compose build episode-sorter && sudo docker compose up -d
sudo docker compose logs -f --tail 50 episode-sorter
```

## Click'n'Load to a JDownloader that is not on your machine

Click'n'Load posts to `http://127.0.0.1:9666`, always on the machine running the browser,
so a JDownloader on the NAS never sees it. The official browser extension was Manifest V2
and stopped working in Chrome in early 2025.

What works without any extension: forward port 9666 from your machine to the NAS. To the
website it then looks like a local JDownloader.

**Windows, once, as administrator, survives reboots:**

```powershell
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=9666 connectaddress=<nas-ip> connectport=9666
netsh interface portproxy show all
netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=9666
```

**macOS or Linux, for as long as the window stays open:**

```bash
ssh -N -L 9666:127.0.0.1:9666 root@<nas-ip>
```

Two things to know:

- A JDownloader running locally occupies port 9666 itself and wins. Close it.
- JDownloader asks once per website whether it may add links. That prompt now appears on
  the NAS, so open the JDownloader window once and choose *always allow*. After that the
  site is on the list in `ExternInterfaceAuth` and the question does not come back.

The container publishes port 9666 for this. It accepts links without authentication, so
keep it inside the LAN.
