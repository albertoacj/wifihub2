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

## Dependências nos aparelhos (OpenWrt)

Para o painel mostrar os clientes por AP, os aparelhos precisam de alguns
pacotes. O próprio assistente detecta o que falta e oferece instalar via opkg
ao testar cada aparelho.

- **iw** — ESSENCIAL nos APs (e no gateway, se ele emitir wifi). É o que lista
  os clientes associados a cada rádio. Sem ele, o AP aparece vazio.
- **iwinfo** — recomendado (info de rádio); costuma já vir em builds com wifi.
- **nlbwmon** — opcional, só para a aba Consumo (tráfego por dispositivo).

Base do OpenWrt (já presente): ubus, uci, dnsmasq, ip.

Instalação manual, se preferir:

```
# OpenWrt novo (24.10+): usa apk
apk add iw iwinfo nlbwmon

# OpenWrt antigo: usa opkg
opkg update && opkg install iw iwinfo nlbwmon
```
