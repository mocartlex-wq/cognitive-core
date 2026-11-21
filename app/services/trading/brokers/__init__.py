"""Реальные брокерские адаптеры.

Каждый реализует протокол BrokerClient из app.services.trading.broker.

Безопасность по умолчанию:
  • alpaca   → paper-api.alpaca.markets  (paper)
  • binance  → testnet.binance.vision    (testnet)
  • tinkoff  → SandboxService/*          (sandbox endpoints)

Live-режим включается ТОЛЬКО при одновременном выполнении:
  1. settings.trading_allow_live = True
  2. settings.{alpaca,binance,tinkoff}_paper = False (для конкретного брокера)

Без обоих флагов — адаптер физически бьёт по песочнице.
"""
