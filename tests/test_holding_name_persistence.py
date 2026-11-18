# -*- coding: utf-8 -*-
"""
持仓名称沉淀(~/.dsa_name_cache.json)行为回归测试。

覆盖:
- name_resolver 新助手 cached_name / remember_names 的读写与容错
- _cache_stock_name 的沉淀门控(仅持仓∪自选、跳过静态表已有代码)
- get_stock_name 在所有数据源无名字时对持仓/自选代码走 name_resolver 兜底
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import data_provider.name_resolver as name_resolver
from data_provider.base import DataFetcherManager
from src.data.stock_mapping import STOCK_NAME_MAP


class _NamelessFetcher:
    """无名字能力的数据源, 用于驱动 get_stock_name 走完回退链。"""

    name = "NamelessFetcher"
    priority = 99

    def get_stock_name(self, _stock_code):
        return None


class HoldingNamePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = os.path.join(self._tmp.name, "dsa_name_cache.json")
        patcher = patch.object(name_resolver, "CACHE_FILE", self.cache_file)
        patcher.start()
        self._patcher = patcher
        self.manager = DataFetcherManager(fetchers=[_NamelessFetcher()])

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _read_cache(self):
        with open(self.cache_file, encoding="utf-8") as f:
            return json.load(f)

    def test_remember_and_cached_roundtrip(self):
        self.assertIsNone(name_resolver.cached_name("160644"))
        name_resolver.remember_names({"160644": "港美互联网LOF"})
        self.assertEqual(name_resolver.cached_name("160644"), "港美互联网LOF")
        name_resolver.remember_names({"160644": "港美互联网LOF"})  # 值相同不重复写
        self.assertEqual(self._read_cache().get("160644"), "港美互联网LOF")

    def test_corrupt_cache_tolerated(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            f.write("not-json{{{")
        self.assertIsNone(name_resolver.cached_name("160644"))
        name_resolver.remember_names({"160644": "港美互联网LOF"})
        self.assertEqual(self._read_cache().get("160644"), "港美互联网LOF")

    def test_persist_scope_is_holdings_union_stock_list(self):
        with patch.object(
            DataFetcherManager, "_get_holding_codes", return_value={"600036"}
        ), patch("src.config.get_config", return_value=SimpleNamespace(stock_list=["513310"])):
            scope = self.manager._get_persist_stock_codes()
        self.assertEqual(scope, {"600036", "513310"})

    def test_tracked_name_persisted(self):
        # 160999 不在静态表, 作用域内解析成功后应沉淀
        with patch.object(
            DataFetcherManager, "_get_persist_stock_codes", return_value={"160999"}
        ):
            self.manager._cache_stock_name("160999", "测试基金LOF")
        self.assertEqual(self._read_cache().get("160999"), "测试基金LOF")

    def test_out_of_scope_name_not_persisted(self):
        with patch.object(
            DataFetcherManager, "_get_persist_stock_codes", return_value={"160999"}
        ):
            self.manager._cache_stock_name("600000", "某非作用域股")
        self.assertFalse(os.path.exists(self.cache_file))

    def test_static_map_code_not_persisted(self):
        static_code = next(iter(STOCK_NAME_MAP))
        with patch.object(
            DataFetcherManager, "_get_persist_stock_codes", return_value={static_code}
        ):
            self.manager._cache_stock_name(static_code, STOCK_NAME_MAP[static_code])
        self.assertFalse(os.path.exists(self.cache_file))

    def test_get_stock_name_rescue_via_name_resolver(self):
        # 160999 不在静态表/指数表, 假源无名字, 作用域内兜底应命中 resolve_names
        with patch.object(
            DataFetcherManager, "_get_persist_stock_codes", return_value={"160999"}
        ), patch.object(
            name_resolver, "resolve_names", return_value={"160999": "测试基金LOF"}
        ):
            got = self.manager.get_stock_name("160999")
        self.assertEqual(got, "测试基金LOF")
        self.assertEqual(self._read_cache().get("160999"), "测试基金LOF")

    def test_get_stock_name_out_of_scope_no_rescue(self):
        # 作用域外代码不触发兜底, 保持原有行为返回 ""
        with patch.object(
            DataFetcherManager, "_get_persist_stock_codes", return_value=set()
        ), patch.object(name_resolver, "resolve_names") as mock_resolve:
            got = self.manager.get_stock_name("160999")
        self.assertEqual(got, "")
        mock_resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
