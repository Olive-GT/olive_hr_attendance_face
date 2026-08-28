#!/usr/bin/env bash
# Aprovisiona un servidor Odoo 18 Enterprise desde cero (Debian 12) con los
# módulos: olive_hr_attendance_face, odoo_aro (contabilidad) y odoo_nomina.
#
# Pensado para poder entregarse tal cual a un cliente que quiera montar su
# propio servidor: solo necesita un Debian 12 limpio, su propio .deb de
# Odoo 18 Enterprise (descargado desde su cuenta de Odoo) y correr este
# script como root.
#
# Uso:
#   sudo ./provision.sh /ruta/al/odoo_18e.deb
#
# Variables opcionales (export antes de correr, o anteponer al comando):
#   REPO_HR_ATTENDANCE_FACE, HR_ATTENDANCE_FACE_REF
#   REPO_ARO, ARO_REF
#   REPO_NOMINA, NOMINA_REF

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Este script debe correr como root (sudo)." >&2
  exit 1
fi

ODOO_DEB_PATH="${1:-}"
if [ -z "$ODOO_DEB_PATH" ] || [ ! -f "$ODOO_DEB_PATH" ]; then
  echo "Uso: $0 /ruta/al/odoo_18e.deb" >&2
  exit 1
fi

REPO_HR_ATTENDANCE_FACE="${REPO_HR_ATTENDANCE_FACE:-https://github.com/Olive-GT/olive_hr_attendance_face.git}"
HR_ATTENDANCE_FACE_REF="${HR_ATTENDANCE_FACE_REF:-main}"
REPO_ARO="${REPO_ARO:-https://github.com/Olive-GT/odoo_aro.git}"
ARO_REF="${ARO_REF:-18.0}"
REPO_NOMINA="${REPO_NOMINA:-https://github.com/Olive-GT/odoo_nomina.git}"
NOMINA_REF="${NOMINA_REF:-main}"

# Dominio opcional: si se define, Nginx queda con ese server_name y se corre
# certbot para HTTPS. Sin dominio, Nginx sirve HTTP en el default_server (_).
DOMAIN="${DOMAIN:-}"

ODOO_BASE=/opt/odoo
CUSTOM_DIR="$ODOO_BASE/custom"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Instalando dependencias del sistema y Odoo 18 Enterprise"
apt-get update -y
# apt (a diferencia de dpkg -i) resuelve Depends/Recommends del .deb:
# PostgreSQL, wkhtmltopdf, node/less, python3-passlib, etc.
apt-get install -y "$ODOO_DEB_PATH" git python3-xlsxwriter

mkdir -p "$CUSTOM_DIR"

clone_or_update() {
  local dir="$1" url="$2" ref="$3"
  if [ -d "$dir/.git" ]; then
    echo "==> Actualizando $dir"
    git -C "$dir" fetch --depth 1 origin "$ref"
    git -C "$dir" checkout "$ref"
    git -C "$dir" reset --hard "origin/$ref"
  else
    echo "==> Clonando $url ($ref) en $dir"
    git clone --depth 1 --branch "$ref" "$url" "$dir"
  fi
}

clone_or_update "$CUSTOM_DIR/olive_hr_attendance_face" "$REPO_HR_ATTENDANCE_FACE" "$HR_ATTENDANCE_FACE_REF"
clone_or_update "$ODOO_BASE/odoo_aro" "$REPO_ARO" "$ARO_REF"
clone_or_update "$ODOO_BASE/odoo_nomina" "$REPO_NOMINA" "$NOMINA_REF"

echo "==> Inicializando submódulo reporting-engine (OCA) dentro de odoo_aro"
git -C "$ODOO_BASE/odoo_aro" submodule update --init --recursive

echo "==> Permisos: el repo es de root, Odoo lo lee (mismo criterio que el resto de la flota)"
chown -R root:root "$CUSTOM_DIR" "$ODOO_BASE/odoo_aro" "$ODOO_BASE/odoo_nomina"
chmod -R a+rX "$CUSTOM_DIR" "$ODOO_BASE/odoo_aro" "$ODOO_BASE/odoo_nomina"

echo "==> Generando contraseña maestra (admin_passwd) nueva para este servidor"
ADMIN_PASSWD_PLAIN="$(openssl rand -base64 24)"
ADMIN_PASSWD_HASH="$(python3 -c "
from passlib.context import CryptContext
ctx = CryptContext(['pbkdf2_sha512'])
import sys
print(ctx.hash(sys.argv[1]))
" "$ADMIN_PASSWD_PLAIN")"

mkdir -p /etc/odoo
sed "s|__ADMIN_PASSWD_HASH__|${ADMIN_PASSWD_HASH}|" "$SCRIPT_DIR/odoo.conf.template" > /etc/odoo/odoo.conf
chown odoo:odoo /etc/odoo/odoo.conf
chmod 640 /etc/odoo/odoo.conf

echo "==> Configurando Ops Agent (métricas reducidas para bajar costo de Cloud Monitoring)"
if [ -d /etc/google-cloud-ops-agent ]; then
  cp "$SCRIPT_DIR/ops-agent-config.yaml" /etc/google-cloud-ops-agent/config.yaml
  systemctl restart google-cloud-ops-agent || true
else
  echo "    (Google Cloud Ops Agent no está instalado; se omite. Es solo para VMs en GCP.)"
fi

echo "==> Reiniciando Odoo"
systemctl enable odoo
systemctl restart odoo

echo "==> Instalando y configurando Nginx como reverse proxy (mismo patrón que el resto de la flota)"
apt-get install -y nginx
SERVER_NAME="${DOMAIN:-_}"
sed "s|__SERVER_NAME__|${SERVER_NAME}|" "$SCRIPT_DIR/nginx-odoo.conf.template" > /etc/nginx/sites-available/odoo
ln -sf /etc/nginx/sites-available/odoo /etc/nginx/sites-enabled/odoo
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

if [ -n "$DOMAIN" ]; then
  echo "==> Dominio detectado ($DOMAIN): pidiendo certificado con certbot"
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "${CERTBOT_EMAIL:-admin@$DOMAIN}" --redirect
fi

cat <<EOF

============================================================
Listo. Odoo 18 Enterprise instalado con:
  - olive_hr_attendance_face  ($CUSTOM_DIR/olive_hr_attendance_face)
  - odoo_aro                  ($ODOO_BASE/odoo_aro)
  - odoo_nomina                ($ODOO_BASE/odoo_nomina)

Contraseña maestra (admin_passwd) de este servidor, GUÁRDALA AHORA,
no se vuelve a mostrar:

  $ADMIN_PASSWD_PLAIN

Servicio: systemctl status odoo / systemctl status nginx
Logs:     /var/log/odoo/odoo-server.log , /var/log/nginx/odoo.*.log
Acceso:   http://IP_DEL_SERVIDOR/ (Nginx en :80 -> Odoo en 127.0.0.1:8069)
$( [ -n "$DOMAIN" ] && echo "HTTPS ya configurado para $DOMAIN via certbot." || echo "Sin dominio todavia: solo HTTP. Cuando haya dominio, correr de nuevo con DOMAIN=tu.dominio.com" )

Falta crear la base de datos e instalar los módulos, por ejemplo:
  sudo -u odoo odoo -c /etc/odoo/odoo.conf -d NOMBRE_BD -i \\
    accountant,olive_hr_attendance_face,l10n_gt_aro_accounting,l10n_gt_payroll_isr,l10n_gt_payroll_loans,l10n_gt_payroll_prestaciones,l10n_gt_payroll_liquidacion,l10n_gt_payroll_account,l10n_gt_payroll_report \\
    --stop-after-init

Nota: 'accountant' es la app de Contabilidad de Enterprise. l10n_gt_aro_accounting
solo depende de 'account' (facturacion), asi que sin instalar 'accountant'
explicitamente la app de Contabilidad no aparece.
============================================================
EOF
