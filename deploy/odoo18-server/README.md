# Servidor Odoo 18 Enterprise — aprovisionamiento

Todo lo necesario para levantar, desde cero, un servidor con Odoo 18 Enterprise
más `olive_hr_attendance_face`, `odoo_aro` (contabilidad GT) y `odoo_nomina`
(nómina GT). Mismo criterio que el resto de la flota: paquete `.deb` +
`systemctl`, sin Docker, para poder entregarlo tal cual si el cliente algún
día quiere su propio servidor.

## Requisitos

- Una VM Debian 12 limpia (en GCP, on-prem, o donde sea), con acceso root.
- El `.deb` de Odoo 18 Enterprise, descargado desde la cuenta de Odoo de quien
  vaya a correr el servidor (cada suscripción tiene su propio link de
  descarga; no se distribuye aquí por licencia).

## Uso

```bash
scp odoo_18e.deb root@SERVIDOR:/root/
scp -r deploy/odoo18-server root@SERVIDOR:/root/
ssh root@SERVIDOR
cd /root/odoo18-server
chmod +x provision.sh
./provision.sh /root/odoo_18e.deb
```

El script instala Odoo y sus dependencias vía `apt`, clona los tres módulos
en `/opt/odoo/`, arma `/etc/odoo/odoo.conf` con el `addons_path` correcto,
genera una contraseña maestra nueva (la imprime una sola vez al final,
guárdala), configura el Ops Agent con métricas reducidas si detecta que está
en GCP, y arranca el servicio `odoo`.

Por defecto clona los repos públicos de `Olive-GT` en GitHub. Para usar otra
fuente (por ejemplo, una copia local entregada al cliente en vez de GitHub),
sobreescribe las variables antes de correr el script:

```bash
REPO_ARO=/root/entrega/odoo_aro ARO_REF=18.0 ./provision.sh /root/odoo_18e.deb
```

(`clone_or_update` en el script asume un remoto git; si se entrega como
carpeta plana sin `.git`, cópiala directamente a `/opt/odoo/odoo_aro` antes
de correr el script y comenta esa línea de `clone_or_update`.)

## Después de correr el script

1. Crear la base de datos e instalar los módulos (ver el mensaje final del
   script, trae el comando exacto).
2. Sigue sirviendo por **HTTP en el puerto 8069**, sin TLS. La verificación
   por cámara (`getUserMedia`) no funciona sin HTTPS — es el mismo pendiente
   que tiene el servidor `inmobiliario`. En cuanto haya un dominio apuntando
   a la IP de este servidor, se agrega Nginx + Certbot (Let's Encrypt) por
   delante del puerto 8069.
3. Backups: si está en GCP, adjuntar una política de snapshot diario al disco
   (`gcloud compute disks add-resource-policies`), igual que las demás VMs
   de `pale-network`.

El script siempre instala Nginx como reverse proxy delante de Odoo (puerto 80 →
127.0.0.1:8069), igual que el resto de la flota. Si defines `DOMAIN` antes de
correrlo, además pide el certificado con certbot y activa HTTPS:

```bash
DOMAIN=odoo18.miempresa.com CERTBOT_EMAIL=tu@correo.com ./provision.sh /root/odoo_18e.deb
```

Sin `DOMAIN`, Nginx queda sirviendo solo HTTP (`server_name _`, catch-all) —
se puede volver a correr el script más tarde con `DOMAIN` para agregar HTTPS
sin tener que reinstalar nada.

## Actualizar un módulo en un servidor que ya está corriendo

```bash
cd /opt/odoo/odoo_aro          # o el repo que toque
sudo git pull --ff-only origin 18.0
sudo chmod -R a+rX /opt/odoo/odoo_aro
sudo systemctl stop odoo
sudo -u odoo odoo -c /etc/odoo/odoo.conf -d NOMBRE_BD -u NOMBRE_MODULO --stop-after-init
sudo systemctl start odoo
```

Dos cosas que ya nos mordieron al actualizar `l10n_gt_aro_accounting`:

**1. Vistas huérfanas cuando cambia un xmlid.** Si un commit renombra el `id`
de una vista y además elimina los campos que la vieja referenciaba, el registro
antiguo queda huérfano en la base y el upgrade revienta con
`Field 'X' does not exist` al validar el árbol de herencia. En instalación
limpia no pasa, solo en las que ya tenían el módulo. Se destraba borrando la
vista vieja antes de actualizar:

```bash
# ojo: con el codigo NUEVO en disco el ORM no arranca (faltan columnas), asi
# que hay que volver temporalmente al commit anterior para poder borrarla
sudo git checkout <commit_anterior>
echo "env.ref('MODULO.xmlid_viejo').unlink(); env.cr.commit()" \
  | sudo -u odoo odoo shell -c /etc/odoo/odoo.conf -d NOMBRE_BD --no-http
sudo git checkout 18.0
```

La solución de fondo es no renombrar el xmlid, o dejar una migración en
`migrations/` que borre el registro viejo.

**2. `post_init_hook` no corre en upgrades**, solo en instalaciones. Si el
commit trae un hook que migra datos históricos, hay que invocarlo a mano:

```bash
echo "from odoo.addons.MODULO.hooks import post_init_hook
post_init_hook(env); env.cr.commit()" \
  | sudo -u odoo odoo shell -c /etc/odoo/odoo.conf -d NOMBRE_BD --no-http
```

## Qué NO incluye este script (a propósito)

- No pide el certificado si no le das `DOMAIN` — depende de que el dominio ya
  apunte a la IP del servidor antes de correr certbot.
- No copia el `.deb` de Odoo Enterprise — cada quien lo descarga de su propia
  cuenta, por licencia.
- No crea la base de datos ni instala los módulos automáticamente — eso
  implica decisiones de datos (nombre de BD, compañía, usuario admin) que no
  deben quedar hardcodeadas en un script de infraestructura.
