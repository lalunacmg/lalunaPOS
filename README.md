> **Polished final release — 18 Nov 2026:** A full visual and functional pass includes 60 app tests, 42 isolated server checks, mandatory public admin tab PIN configuration, improved back-office alignment and a read-only Render go-live checker. Begin with [GITHUB_RENDER_GO_LIVE_2026_11_18.md](GITHUB_RENDER_GO_LIVE_2026_11_18.md); see [RELEASE_VERIFICATION_2026_11_18.md](RELEASE_VERIFICATION_2026_11_18.md) for the detailed verification scope.

> **NEW — Orders payment, discounts, cash register notes & admin tab PIN (17 Nov 2026):** POS orders are staged unpaid; staff select payment and any manual Senior/PWD discount in **Orders**, with cash denomination buttons and change calculation. A separate environment-only admin PIN protects selected tabs. See [PAYMENT_DISCOUNTS_ADMIN_ACCESS_RENDER.md](PAYMENT_DISCOUNTS_ADMIN_ACCESS_RENDER.md).

> **NEW — POS-only items and custom product categories (17 Nov 2026):** In **Back Office → Products**, create categories and choose whether each product appears on the customer QR menu, staff POS, or both. The schema migration is additive; permanent table QRs stay the same. See [PRODUCT_CATEGORIES_AND_VISIBILITY_RENDER.md](PRODUCT_CATEGORIES_AND_VISIBILITY_RENDER.md) for Render update and backup instructions.

> **POS / Tables fix (17 Nov 2026):** In the real back office, unpaid table tickets now display immediately on the table's Open tickets count, while completed paid POS sales appear separately under Paid sales/revenue. See [POS_TABLE_ORDER_FIX.md](POS_TABLE_ORDER_FIX.md) for correct setup and GitHub/Render deployment. The standalone interactive preview is *demo-only* and never writes restaurant orders.

> **NEW — Tables, Employees, Cash Register & Dashboard (17 Nov 2026):** See [TABLES_EMPLOYEES_REGISTER_UPDATE.md](TABLES_EMPLOYEES_REGISTER_UPDATE.md) for installation, employee PIN setup, cash denomination counting, table-specific POS orders, analytics, and Render deployment steps.

# La Luna Grill + Cafe — Public GitHub release

Customer ordering and restaurant back office powered by **FastAPI + SQLite**. This repository is suitable for public source hosting on GitHub. **GitHub Pages is static only** and does not run this Python server; deploy `server.py` on Render or another Python host for a working customer checkout and admin back office.

**Routes on the running Python server**

| Page | Path |
| --- | --- |
| Customer ordering | `/` |
| Staff and back office | `/backoffice` |
| Phone/QR connectivity test | `/qr-test` |
| Health check | `/api/health` |

Customers can scan table-specific QR codes on mobile data or any Wi-Fi **after** the Python app has been deployed to a public HTTPS URL. The customer site and back office then share one server and one database. New orders go directly to Back Office → Orders; checkout does **not** submit to Facebook Messenger.

## Latest table QR checkout repair

The customer **Review & Submit Order** button was previously disabled by an exact version-label mismatch between `static/storefront-upgrade.js` and `server.py`. This version checks a stable `checkout_protocol` and available order types instead. **Deploy all updated files, including `server.py`, `static/index.html` and `static/storefront-upgrade.js`, to the same Python host.** The included GitHub Pages `docs/` entry redirects to that host; publishing only `static/index.html` on GitHub Pages cannot submit orders. See [QR_CHECKOUT_FIX.md](QR_CHECKOUT_FIX.md).

## Already have a GitHub Pages site?

This release includes a **static GitHub Pages entry point** under [`docs/`](docs/). It forwards visitors, including scanned table QR links, to the real Python application. First deploy the Python app, then set its HTTPS URL in [`docs/config.js`](docs/config.js) and configure GitHub Pages to publish **`main` → `/docs`**. Do not publish `static/index.html` as a standalone customer checkout: it needs the API.

## Deploy

Follow [DEPLOYMENT.md](DEPLOYMENT.md) for exact steps: public repo, Render Blueprint, paid persistent disk, admin credentials, phone QR test, existing database migration, and GitHub Pages setup.

Basic layout:

```text
server.py                # FastAPI: customer page, admin page, API
render.yaml              # Render web service, persistent SQLite disk
.python-version          # Python runtime for Render and GitHub CI
requirements.txt
static/                  # Served by the Python app (do not use directly as GitHub Pages)
docs/                    # Optional GitHub Pages landing and redirect
.github/workflows/ci.yml # Public-repository automated tests and checks
START_LA_LUNA_ALL_IN_ONE.bat  # Windows local development/operations only
```

## Test the software locally

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
uvicorn server:app --host 127.0.0.1 --port 8000
```

The local admin needs an owner account. Configure `LALUNA_ADMIN_EMAIL` and `LALUNA_ADMIN_PASSWORD` (12+ characters) in your local **environment**, not in source code. On Windows, use the all-in-one launcher in this folder. Do not run this local-only deployment as if it were the public QR server.

## Existing restaurant data and security

A new Render deployment creates a **new** database; it does **not** automatically synchronize with a local `laluna.db`. Back up and migrate existing records safely or start fresh. Never commit your database, backups, `.env`, credentials, customer contact details, or private keys. See [SECURITY.md](SECURITY.md). This release includes staff login and CSRF checks but **does not include MFA or a professional penetration test**. Review access controls and business-data privacy before going live.

Printing directly to a Bluetooth receipt printer paired to your PC requires the local Windows machine; a Render-hosted Python server cannot talk directly to that local device. Browser print can work from a PC with an installed printer driver.

This public repository is source code, **not proof the restaurant is publicly deployed**. A live URL exists only after completing the hosting steps and verifying it on actual devices.

> **Licensing:** No open-source license has been chosen. Making a repository public exposes its code but does not automatically grant others permission to reuse it. Add a license only if you choose to grant reuse rights.

## Latest update: iPad/Android receipt printing and staff POS delivery (17 November 2026)

Read **[TABLET_RENDER_DEPLOYMENT.md](TABLET_RENDER_DEPLOYMENT.md)** for step-by-step existing-Render deployment, printer compatibility, iPad AirPrint, Android BLE connection, receipt printing on every order, delivery POS fee and address, tablet setup, backups, operational tests and troubleshooting.

**Printer reality:** iPad Safari does not connect to generic Bluetooth-only thermal printers directly. Use an AirPrint-enabled Wi-Fi printer or printer manufacturer's iOS app. Android Chrome offers direct BLE ESC/POS printing only to supported GATT profiles; classic Bluetooth printers require a device print-service/vendor app. Render cannot directly access your tablet's Bluetooth radio.

### Permanent public table QR codes (2026-11-17)

Every configured restaurant table has an immutable, database-backed QR key. Generate repeat-use table QR codes only from the **public HTTPS** back office. A generated QR targets `https://YOUR-REAL-DOMAIN/t/<table-key>`, not a LAN IP or mutable `?table=T1` link. On every scan, the server resolves the latest table number for Dine-In checkout; Take-Out remains available. Reprint all previous local-IP table QR signs once, but newly generated permanent QRs can be reused daily and survive normal table renaming as long as the domain and persistent database survive. See [PERMANENT_PUBLIC_QR_DEPLOYMENT.md](PERMANENT_PUBLIC_QR_DEPLOYMENT.md) before redeploying.

## Latest back-office upgrade (2026-11-17)

POS now supports **optional employee attribution (printed on receipts), category filtering, and editing one existing unpaid table ticket without creating duplicate orders**. Owners/managers can safely archive inactive tables while retaining sales history. For exact behavior, safety restrictions, deployment instructions and production tests, see [POS_EMPLOYEE_TABLE_TICKETS_UPDATE.md](POS_EMPLOYEE_TABLE_TICKETS_UPDATE.md).

## Permanent table QR codes now auto-sync with live menu (17 November 2026)

Printed HTTPS table QRs continue to use the same stable `/t/<table-key>` URLs. When the owner edits Products in the hosted back office, all new scans load the updated database menu, and customers who already have the page open get automatic updates every ~15 seconds (or when they return to the tab). Cart prices and availability are reconciled before checkout; an affected order requires a fresh review. See [PERMANENT_QR_LIVE_MENU_SYNC.md](PERMANENT_QR_LIVE_MENU_SYNC.md) for Render deployment and validation.


## Product images — new November 19 update

In **Back Office → Products**, create or edit any item and use **Upload / replace photo**.
Save the product; the picture is resized to 1200 px maximum and stored as WebP.
Existing images can be removed; the menu reverts to the product emoji automatically.
JPG, PNG, WebP, GIF (first frame), and HEIC/HEIF from iPad/iPhone are supported.
File limit: 8 MB; decoded pixel limit: 24 megapixels.

Images are served from `/media/products/<random>.webp`. Customer-visible products
show their images on the public menu and in the item popup; POS-only product
photos are accessible only to authenticated staff. Product photo changes trigger
live menu revisions; **permanent table QR codes are not changed or reprinted**.

**Render persistence:** Uploads are NOT in `static/` or GitHub. They are stored by
`LALUNA_MEDIA_DIR`, defaulting to `<directory containing LALUNA_DB_PATH>/laluna-media`.
With `LALUNA_DB_PATH=/var/data/laluna.db`, photo files reside in
`/var/data/laluna-media/products/`, on the **same persistent Render disk**.
An optional explicit `LALUNA_MEDIA_DIR=/var/data/laluna-media` is equivalent.
Back up photos as well as the SQLite database: `python laluna_diagnostics.py --backup`
creates a consistent SQLite backup and, when photos exist, a separate ZIP of
uploaded photos. Copy BOTH backups off Render; neither is stored in GitHub.

See `PRODUCT_PHOTOS_RENDER_DEPLOYMENT.md` for complete deployment and rollback.
