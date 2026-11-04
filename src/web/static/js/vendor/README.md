# Vendored third-party assets

## qrcode.min.js
- **Library:** qrcode-generator v1.4.4 by Kazuhiko Arase
- **License:** MIT
- **Source:** https://github.com/kazuhikoarase/qrcode-generator
- **Why vendored:** Mobile Command renders QR codes client-side (the MagicDNS URL
  is dynamic per tailnet). Vendoring keeps it fully offline with zero server-side
  dependency and no runtime CDN fetch.
- **API used:** `qrcode(0, 'M')` → `.addData(text)` → `.make()` → `.createSvgTag({...})`
