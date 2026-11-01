# mysee

Webserver minimale di videosorveglianza: trova tutte le telecamere V4L2 del
sistema (`/dev/video*`) e le mostra in una griglia stile CCTV.

- `GET /` – griglia live (clic su una telecamera = schermo intero, Esc per tornare)
- `GET /stream/<id>` – stream MJPEG (es. `/stream/video0`)
- `GET /snapshot/<id>` – singolo JPEG
- `GET /api/cameras` – elenco telecamere in JSON

Ogni telecamera viene aperta solo quando qualcuno la guarda e chiusa dopo 10 s
senza spettatori. Le telecamere collegate o scollegate vengono rilevate ogni 15 s.
La cattura passa da libv4l2 (`v4l2-ctl -w`), quindi funziona anche con webcam
vecchie che usano formati proprietari (es. gspca/spca561).

Dipendenze sulla macchina: `python3` (>= 3.9), `python3-pil`, `v4l-utils`.
Nessun login: da usare solo in LAN.

## Deploy

    ./deploy.sh                              # francesco@192.168.144.44
    MYSEE_HOST=utente@host ./deploy.sh

Installa in `/opt/mysee` e crea il servizio systemd `mysee` sulla porta 8080:

    systemctl status mysee
    journalctl -u mysee -f

## Avvio locale

    python3 mysee/server.py --port 8080 -v
