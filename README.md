# PokéHunter Cloud Gateway

Lehká HTTP brána (`curl_cffi` + `aiohttp`) pro stahování výpisů z e-shopů chráněných Cloudflare (Smarty.cz, Alza.cz) ze Supabase Edge Function `monitor`.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Taxenek/poketracker-cloud)

## Konfigurace

- `POKETRACKER_CRON_TOKEN` *(povinné)* – shodný token jako `settings.cron_token` v Supabase.
- `POKETRACKER_PROXY` *(volitelné)* – HTTP/HTTPS proxy (`http://user:pass@host:port`).
- `POKETRACKER_IMPERSONATE` *(volitelné)* – výchozí `chrome` (nejnovější profil v `curl_cffi`).
