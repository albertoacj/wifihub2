# Changelog

## 0.1.16
- Corrige o consumo de **Semana** e **Mês**, que vinham zerados: passam a ler
  do bucket ao vivo (retenção de 30 dias cobre os dois) em vez do bucket
  histórico, que dependia da task de downsampling. O agregado de 7 dias volta
  a funcionar. (O período **Ano** continua usando o histórico permanente.)

## 0.1.15
- `nlbwmon` deixa de ser exigido nos APs (dumb AP bridgeia o tráfego; o
  consumo por dispositivo é medido no gateway). O aviso "falta: nlbwmon"
  some dos access points e continua só no gateway.
- O botão Instalar agora mostra a mensagem de erro real do apk/opkg quando
  a instalação falha, em vez de só repetir "ainda falta".

## 0.1.14
- Adiciona a aba **Config** na barra de navegação do painel, que abre o
  assistente (gateway + APs: adicionar/remover, testar e instalar a chave).
  A tela já existia em /setup, mas não havia link para chegar nela pelo
  dashboard.

## 0.1.0
- Primeira versão (Fase A): esqueleto do add-on com Ingress (barra lateral),
  InfluxDB 2.x embutido (ou externo via opções) e modo setup.
