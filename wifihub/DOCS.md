# WifiHub — Add-on de Home Assistant

Painel de gerência e gráficos de uso para um gateway + APs OpenWrt, acessível
pela barra lateral do Home Assistant (via Ingress).

## Uso interno
Não foi desenhado para exposição à internet. Rodando como add-on via Ingress,
o próprio Home Assistant cuida do acesso e da autenticação — não há porta
exposta na rede.

## InfluxDB
Por padrão, o add-on **embute** seu próprio InfluxDB 2.x (dados em `/data`,
credenciais geradas no 1º boot). Se você já tem um InfluxDB **2.x** e quer
reutilizá-lo, preencha em Configuração:
- `influxdb_url` (ex.: `http://IP:8086`)
- `influxdb_token`, `influxdb_org`, `influxdb_bucket`

Deixe `influxdb_url` em branco para usar o embutido.

## Idioma
`language`: en, pt, uk, fr, de.
