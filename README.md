# Coca Blind Test

Petite application web pour organiser une dégustation à l'aveugle **Coca-Cola normal vs Coca-Cola Zero** entre amis, et voir si on est vraiment capable de faire la différence.

- L'admin configure l'expérience (répartition Coca / Coca Zero dans 100 gobelets A1 → E20).
- L'app guide l'admin gobelet par gobelet pour préparer les verres.
- Chaque participant se connecte avec son prénom, l'app lui indique quel gobelet prendre, il vote **Normal** ou **Zero** et indique sa confiance (1 → 5).
- Une fois publiés par l'admin, les résultats sont visibles par tout le monde avec un classement et plusieurs graphiques (précision globale, par participant, par niveau de confiance, matrice de confusion).

## Stack

- Python 3.12 + Flask
- SQLite (fichier unique, mode WAL)
- Chart.js (CDN) pour les graphes
- Gunicorn + Docker pour la prod

## Démarrage rapide (dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

L'app affiche l'URL locale et l'URL LAN au démarrage :
```
  Local:   http://127.0.0.1:5000
  LAN:     http://192.168.x.x:5000
```

- Interface participant : `/`
- Interface admin : `/admin/login` (mot de passe par défaut : `admin`)

## Déploiement Docker

1. Configure les secrets :
   ```bash
   cp .env.example .env
   # Génère un SECRET_KEY aléatoire :
   python3 -c "import secrets; print(secrets.token_hex(32))"
   # Édite .env pour définir ADMIN_PASSWORD et SECRET_KEY
   ```

2. Lance :
   ```bash
   docker compose up -d --build
   ```

3. Accès : `http://<ip-hôte>:8080` (change le port dans `docker-compose.yml` si besoin).

Les données sont persistées dans `./data/experience.db` sur l'hôte.

### Mise à jour

```bash
git pull && docker compose up -d --build
```

Les données du volume `./data` sont conservées.

### HTTPS avec Caddy

Sur le VPS, installe Caddy puis ajoute à `/etc/caddy/Caddyfile` :
```
degustation.ton-domaine.com {
    reverse_proxy 127.0.0.1:8080
}
```
`systemctl reload caddy` → HTTPS auto (Let's Encrypt).

## Configuration

Variables d'environnement :

| Variable         | Défaut                 | Rôle                                       |
| ---------------- | ---------------------- | ------------------------------------------ |
| `ADMIN_PASSWORD` | `admin`                | Mot de passe de `/admin/login`             |
| `SECRET_KEY`     | `dev-secret-change-me` | Signature des cookies de session Flask     |
| `DB_PATH`        | `./experience.db`      | Chemin du fichier SQLite                   |
| `PORT`           | `5000`                 | Port d'écoute (mode dev uniquement)        |

## Accès depuis le LAN

- **Linux natif / VPS** : ouvre le port dans le pare-feu, ex. `sudo ufw allow 8080/tcp`.
- **WSL2** : port forwarding depuis Windows requis. Dans PowerShell admin :
  ```powershell
  $wsl_ip = (wsl hostname -I).Trim().Split()[0]
  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=$wsl_ip connectport=8080
  New-NetFirewallRule -DisplayName "Coca 8080" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
  ```

## Déroulé d'une soirée type

1. **Admin** se connecte à `/admin/login`.
2. **Configurer** : nombre de gobelets par personne (10 par défaut) + répartition Normal / Zero.
3. **Mise en place** : l'app affiche chaque gobelet A1 → E20 dans l'ordre avec ce qu'il faut verser dedans (bouton *Suivant*).
4. **Les amis** se connectent à l'URL LAN, tapent leur prénom, dégustent et votent.
5. **Publier les résultats** depuis l'admin → tout le monde voit le classement et les graphiques sur `/results`.

## Licence

MIT
