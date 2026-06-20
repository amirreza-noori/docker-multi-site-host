# Direct file transfer

When source and destination can reach each other over SSH.

## rsync (incremental, resumable)

```bash
# WordPress files (exclude cache and backups)
rsync -avz --progress \
  --exclude 'wp-content/cache/' \
  --exclude 'wp-content/upgrade/' \
  --exclude '*.sql' \
  --exclude '*.sql.zip' \
  user@source.example.com:/var/www/example.com/ \
  /var/www/example.com/

# Static site
rsync -avz --progress \
  user@source.example.com:/sites/my-site/public/ \
  /sites/my-site/public/
```

## scp (single archive)

```bash
# On source — pack uploads
tar -czf example-uploads.tar.gz -C /var/www/example.com/wp-content uploads

# Copy to destination
scp example-uploads.tar.gz user@dest.example.com:/tmp/

# On destination — unpack
tar -xzf /tmp/example-uploads.tar.gz -C /var/www/example.com/wp-content/
```

## Full site folder (this stack)

```bash
rsync -avz --progress \
  user@source:/path/to/sites/example-com/ \
  /path/to/sites/example-com/
```

[← Back to index](README.md)
