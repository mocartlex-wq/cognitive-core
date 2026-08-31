#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск из любой директории: python3 путь/к/agroscan/cli.py run parcels/....yaml

`python -m agroscan` работает только из этой папки: в корне репозитория лежит
каталог с тем же именем, и Python подхватывает его как пространство имён.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
from agroscan.__main__ import main

if __name__ == '__main__':
    sys.exit(main(sys.argv))
