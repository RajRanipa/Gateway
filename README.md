# Orient ERP Production Gateway

This service provides durable delivery from the Siemens PLC to the ERP:

1. PLC freezes a product snapshot and sets `NodeJS_Read` to `1`.
2. `gateway_main.py` commits the event to SQLite with `synchronous=FULL`.
3. Only after the commit, the Pi writes `NodeJS_Read=2` and verifies it.
4. `sender_service.py` sends leased queue rows to the ERP.
5. A row becomes `SENT` only after the ERP acknowledges that exact `recordId`.

Internet access is not required for capture. Pending records remain in SQLite
and are retried with bounded exponential backoff.

## Install on the Pi

```bash
cd /home/raj_pi_8616/gateway
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create `/etc/default/pi-gateway`:

```bash
PLC_IP=192.168.1.108
GATEWAY_ID=pi-gateway-1
GATEWAY_NODE_URL=https://api.orientfibertech.com/gateway/blanket/production
GATEWAY_KEY=replace-with-the-same-secret-used-on-render
GATEWAY_SQLITE_PATH=/home/raj_pi_8616/gateway/state/queue.db
GATEWAY_DATA_DIR=/home/raj_pi_8616/gateway/data
GATEWAY_SEND_THRESHOLD=1
```

With `GATEWAY_SEND_THRESHOLD=1`, every completed PLC record is eligible for
delivery immediately after the durable SQLite commit. The sender polls every
two seconds by default. If the network or backend was unavailable, recovered
records may still be delivered together in a small batch.

Protect it:

```bash
sudo chown root:root /etc/default/pi-gateway
sudo chmod 600 /etc/default/pi-gateway
```

Install the two units from `systemd/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-gateway-plc.service
sudo systemctl enable --now pi-gateway-sender.service
```

Deploy with `rsync`, but always exclude `state/`, `data/`, `.env`, `venv/`,
and `.git/`.
The queue database is production data and must never be replaced by deployment.

## Required deployment order

1. Deploy the updated Node.js backend.
2. Run `npm run audit:gateway-module`, review it, then run
   `npm run migrate:gateway-module`.
3. Confirm `GET /gateway/test` succeeds with `X-Gateway-Key`.
4. Set the same `GATEWAY_KEY` on the Pi.
5. Stop both old Pi services.
6. Back up `queue.db`, deploy this source, and start the capture service.
7. Verify new captures enter `PENDING`.
8. Start the sender and verify contract-v2 per-record acknowledgements.
9. After Item and warehouse matching is verified, set
   `GATEWAY_RECONCILE_ENABLED=true` on the backend to enable automatic repair
   of stored production records whose inventory posting is pending.

The backend posts to Item Master / Inventory V2 by default. Keep
`GATEWAY_LEGACY_INVENTORY_ENABLED` unset or `false`. Set it to `true` only when
an explicitly planned legacy dual-write window is required.

If the new Pi sender reaches an old backend, it deliberately keeps records
pending because the old response does not acknowledge individual record IDs.

## Operational checks

```bash
systemctl status pi-gateway-plc.service --no-pager
systemctl status pi-gateway-sender.service --no-pager
journalctl -u pi-gateway-plc.service -f
journalctl -u pi-gateway-sender.service -f
sqlite3 state/queue.db \
  "select status,count(*) from records group by status;"
```

`QUARANTINED` means the backend explicitly rejected a record as non-retryable.
The row remains on disk for correction and controlled replay.
SENT rows are retained indefinitely by default. Configure
`GATEWAY_SENT_RETENTION_DAYS` only after an external backup policy is operating.

## Verification

```bash
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
```

git fetch origin
git pull --ff-only origin main
git rev-parse --short HEAD

sudo systemctl restart pi-gateway-plc.service
sudo systemctl restart pi-gateway-sender.service

sudo systemctl status pi-gateway-plc.service --no-pager
sudo systemctl status pi-gateway-sender.service --no-pager