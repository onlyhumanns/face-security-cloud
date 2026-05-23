# 🚀 Panduan Deploy Face Security ke Railway

## Arsitektur

```
[Komputer Lokal]                    [Railway Cloud]
 Kamera + Deteksi                    Dashboard Web
 agent_lokal.py   ──── HTTPS ──►    app_cloud.py
                                          │
                                    [Browser User]
```

---

## BAGIAN 1 — Deploy ke Railway

### Langkah 1: Siapkan folder untuk Railway

Buat folder baru (khusus untuk Railway, bukan seluruh proyek):

```
face_security_cloud/
├── app_cloud.py
├── requirements.txt
├── Procfile
├── railway.toml
├── .gitignore
└── templates/
    └── index.html
```

Salin file-file dari hasil download ini ke folder tersebut.

---

### Langkah 2: Upload ke GitHub

```bash
cd face_security_cloud
git init
git add .
git commit -m "first deploy"
gh repo create face-security-cloud --public --push --source=.
```

> Jika belum punya GitHub CLI, bisa buat repo manual di github.com lalu push.

---

### Langkah 3: Deploy di Railway

1. Buka **https://railway.app** → Login
2. Klik **New Project** → **Deploy from GitHub repo**
3. Pilih repo `face-security-cloud`
4. Railway otomatis detect `Procfile` dan deploy

---

### Langkah 4: Tambah Environment Variables di Railway

Masuk ke proyek Railway → tab **Variables** → tambahkan:

| Key | Value |
|-----|-------|
| `SECRET_KEY` | string acak panjang (contoh: `xK9mP2qR7nL4wZ1j`) |
| `AGENT_SECRET` | `agent_rahasia_123` (bebas, asal sama dengan di lokal) |

---

### Langkah 5: Catat URL Railway

Setelah deploy selesai, Railway memberi URL seperti:
```
https://face-security-cloud-production.up.railway.app
```

Catat URL ini — akan dipakai di `agent_lokal.py`.

---

## BAGIAN 2 — Jalankan Agent Lokal

### Langkah 1: Salin file ke folder face_security_web

```
face_security_web/
├── agent_lokal.py      ← salin ke sini
├── .env                ← update file ini
├── config.py
├── database.py
├── notifier.py
└── models/
    ├── face_cnn_model.h5
    └── ...
```

---

### Langkah 2: Update .env lokal

Buka `.env` di folder `face_security_web/` dan tambahkan:

```env
RAILWAY_URL=https://face-security-cloud-production.up.railway.app
AGENT_SECRET=agent_rahasia_123
```

> Pastikan `AGENT_SECRET` sama persis dengan yang diisi di Railway Variables.

---

### Langkah 3: Jalankan agent

```bash
cd face_security_web
python agent_lokal.py
```

Output yang benar:
```
[10:00:00] INFO Agent lokal → Railway: https://...
[10:00:00] INFO Memuat model...
[10:00:02] INFO Model siap! Kelas: ['kiels', 'unknown']
[10:00:02] INFO Kamera aktif, mulai deteksi...
```

---

## BAGIAN 3 — Verifikasi

1. Buka URL Railway di browser
2. Dashboard harus menampilkan **"Sistem Aktif"** setelah agent berjalan
3. Ketika orang asing terdeteksi:
   - Dashboard di Railway menampilkan alert + suara
   - Foto dikirim dan muncul di grid snapshot
   - Notifikasi Telegram tetap dikirim dari lokal

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| Dashboard tidak update | Cek `RAILWAY_URL` di `.env` lokal, pastikan tanpa `/` di akhir |
| `403 Unauthorized` di log | Pastikan `AGENT_SECRET` sama di Railway Variables dan `.env` lokal |
| Snapshot tidak muncul | Cek koneksi internet komputer lokal saat upload |
| Railway build gagal | Cek tab **Deployments** di Railway → lihat log error |
| Port error | Railway otomatis inject `$PORT`, jangan hardcode port |

---

## Catatan Penting

- **Model `.h5`, `.dat`, `.npy`** — JANGAN di-push ke GitHub (sudah di `.gitignore`)
- **`.env`** — JANGAN di-push ke GitHub
- Railway **free tier** punya limit 500 jam/bulan — cukup untuk testing
- Snapshot disimpan **in-memory** di Railway (hilang saat restart), foto asli tetap ada di lokal
