"""真實持倉來源層的純邏輯測試（不需要 ARK 或券商 API，任何平台可跑）"""
import datetime as dt
import json
import os
import tempfile
import unittest

import source


def write_config(directory, data):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


def write_csv(directory, text, encoding="utf-8-sig", name="positions.csv"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    return path


COLUMNS = {"code": "股票代號", "qty": "股數", "price": "成交均價"}
CSV_TEXT = "股票代號,股數,成交均價\n2330,50,2062.38\n0050,17,96.76\n"


def write_xlsx(directory, rows, name="positions.xlsx"):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = os.path.join(directory, name)
    wb.save(path)
    return path


class TestLoadConfig(unittest.TestCase):
    def test_未設定時報_SetupRequired(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(source.SetupRequired):
                source.load_config(os.path.join(d, "config.json"))

    def test_讀取設定自動遷移為_v2(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_config(d, {"source": "none"})
            self.assertEqual(source.load_config(path),
                             {"version": 2, "accounts": []})


class TestConfigMigration(unittest.TestCase):
    def test_v1_shioaji_遷移為帳戶清單(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = source.load_config(write_config(d, {"source": "shioaji"}))
            self.assertEqual(cfg["version"], 2)
            self.assertEqual(cfg["accounts"], [{"type": "shioaji", "name": "永豐"}])

    def test_v1_csv_遷移保留路徑與欄位(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = source.load_config(write_config(
                d, {"source": "csv", "csv": {"path": "/p.csv", "columns": COLUMNS}}))
            self.assertEqual(cfg["accounts"],
                             [{"type": "file", "name": "CSV",
                               "path": "/p.csv", "columns": COLUMNS}])

    def test_遷移保留其他設定鍵(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = source.load_config(write_config(
                d, {"source": "shioaji", "external_cash": 14367.0}))
            self.assertEqual(cfg["external_cash"], 14367.0)

    def test_遷移後寫回檔案(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_config(d, {"source": "shioaji"})
            source.load_config(path)
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self.assertEqual(raw["version"], 2)
            self.assertNotIn("source", raw)

    def test_v2_原樣載入不再改寫(self):
        v2 = {"version": 2, "accounts": [{"type": "shioaji", "name": "永豐"}]}
        with tempfile.TemporaryDirectory() as d:
            path = write_config(d, v2)
            self.assertEqual(source.load_config(path), v2)

    def test_不明來源報錯(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_config(d, {"source": "quantum"})
            with self.assertRaises(RuntimeError):
                source.load_config(path)

    def test_帳戶名重複報錯(self):
        v2 = {"version": 2, "accounts": [
            {"type": "file", "name": "A", "path": "/a.csv", "columns": COLUMNS},
            {"type": "file", "name": "A", "path": "/b.csv", "columns": COLUMNS}]}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "重複"):
                source.load_config(write_config(d, v2))

    def test_不明帳戶型別報錯(self):
        v2 = {"version": 2, "accounts": [{"type": "quantum", "name": "A"}]}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                source.load_config(write_config(d, v2))


class TestMergePositions(unittest.TestCase):
    def test_加權平均合併(self):
        merged = source.merge_positions({"永豐": {"2330": (1000, 600.0)},
                                         "國泰": {"2330": (500, 660.0)}})
        self.assertEqual(merged, {"2330": (1500, 620.0)})

    def test_無重疊代號取聯集(self):
        merged = source.merge_positions({"永豐": {"2330": (50, 2062.38)},
                                         "國泰": {"0050": (17, 96.76)}})
        self.assertEqual(merged, {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_單帳戶直通(self):
        pos = {"2330": (50, 2062.38)}
        self.assertEqual(source.merge_positions({"永豐": pos}), pos)

    def test_合併後股數為整數均價為浮點(self):
        merged = source.merge_positions({"a": {"2330": (1, 100.0)},
                                         "b": {"2330": (2, 100.0)}})
        qty, price = merged["2330"]
        self.assertIsInstance(qty, int)
        self.assertIsInstance(price, float)

    def test_全空回傳空dict(self):
        self.assertEqual(source.merge_positions({}), {})


class TestReadFilePositions(unittest.TestCase):
    def test_csv_副檔名走既有解析(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, CSV_TEXT)
            self.assertEqual(source.read_file_positions(path, COLUMNS),
                             {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_xlsx_基本解析(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_xlsx(d, [["股票代號", "股數", "成交均價"],
                                  ["2330", "50", "2062.38"],
                                  ["0050", "17", "96.76"]])
            self.assertEqual(source.read_file_positions(path, COLUMNS),
                             {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_xlsx_數字儲存格(self):
        """Excel 的股數／均價常是數字格式而非文字，代號也可能被存成數字"""
        with tempfile.TemporaryDirectory() as d:
            path = write_xlsx(d, [["股票代號", "股數", "成交均價"],
                                  [2330, 50, 2062.38]])
            self.assertEqual(source.read_file_positions(path, COLUMNS),
                             {"2330": (50, 2062.38)})

    def test_xlsx_錯誤列報錯不靜默(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_xlsx(d, [["股票代號", "股數", "成交均價"],
                                  ["2330", 100.5, 100]])
            with self.assertRaisesRegex(RuntimeError, "非負整數"):
                source.read_file_positions(path, COLUMNS)

    def test_不支援的副檔名報錯(self):
        with self.assertRaisesRegex(RuntimeError, "副檔名"):
            source.read_file_positions("/tmp/p.txt", COLUMNS)

    def test_sniff_headers_支援_xlsx(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_xlsx(d, [["股票代號", "股數", "成交均價"]])
            self.assertEqual(source.sniff_headers(path), ["股票代號", "股數", "成交均價"])


class TestStaging(unittest.TestCase):
    BY_ACCOUNT = {"永豐": {"2330": (1000, 600.0)}, "國泰": {"2330": (500, 660.0)}}
    NOW = dt.datetime(2026, 8, 10, 10, 0, 0)
    TODAY = dt.date(2026, 8, 10)

    def test_寫入後當日載回(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "staging.json")
            source.write_staging(self.BY_ACCOUNT, path=path, now=self.NOW)
            staging = source.load_staging(path=path, today=self.TODAY)
            self.assertEqual(staging["merged"], {"2330": (1500, 620.0)})
            self.assertEqual(staging["accounts"]["永豐"], {"2330": (1000, 600.0)})

    def test_快照不存在時報_StagingRequired(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(source.StagingRequired, "ark-collect"):
                source.load_staging(path=os.path.join(d, "staging.json"),
                                    today=self.TODAY)

    def test_跨日過期拒絕(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "staging.json")
            source.write_staging(self.BY_ACCOUNT, path=path,
                                 now=dt.datetime(2026, 8, 9, 15, 0, 0))
            with self.assertRaisesRegex(source.StagingRequired, "2026-08-09"):
                source.load_staging(path=path, today=self.TODAY)

    def test_空帳戶拒絕寫入(self):
        """任一帳戶 0 檔多半是讀取失敗——寫入端守門，不讓它變成刪光庫存的快照"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "staging.json")
            with self.assertRaisesRegex(RuntimeError, "0 檔"):
                source.write_staging({"永豐": {}}, path=path, now=self.NOW)
            self.assertFalse(os.path.exists(path))

    def test_載回的持倉為tuple型別(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "staging.json")
            source.write_staging(self.BY_ACCOUNT, path=path, now=self.NOW)
            staging = source.load_staging(path=path, today=self.TODAY)
            qty, price = staging["merged"]["2330"]
            self.assertIsInstance(qty, int)
            self.assertIsInstance(price, float)


class TestAccountHelpers(unittest.TestCase):
    SHIOAJI = {"type": "shioaji", "name": "永豐"}
    FILE = {"type": "file", "name": "國泰", "path": "/p.csv",
            "columns": {"code": "c", "qty": "q", "price": "p"}}

    def test_純ARK判定(self):
        self.assertTrue(source.is_pure_ark({"version": 2, "accounts": []}))
        self.assertFalse(source.is_pure_ark({"version": 2, "accounts": [self.SHIOAJI]}))

    def test_只有永豐不需要staging(self):
        self.assertFalse(source.needs_staging({"version": 2, "accounts": [self.SHIOAJI]}))

    def test_有檔案帳戶就需要staging(self):
        for accounts in ([self.SHIOAJI, self.FILE], [self.FILE]):
            self.assertTrue(source.needs_staging({"version": 2, "accounts": accounts}))


class TestReadCsvPositions(unittest.TestCase):
    def test_基本解析(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, CSV_TEXT)
            self.assertEqual(source.read_csv_positions(path, COLUMNS),
                             {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_千分位與空白(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,\" 1,000 \",\" 2,062.38 \"\n")
            self.assertEqual(source.read_csv_positions(path, COLUMNS),
                             {"2330": (1000, 2062.38)})

    def test_cp950_編碼(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, CSV_TEXT, encoding="cp950")
            self.assertEqual(source.read_csv_positions(path, COLUMNS),
                             {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_空代號列跳過(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,50,100\n,10,10\n")
            self.assertEqual(source.read_csv_positions(path, COLUMNS),
                             {"2330": (50, 100.0)})

    def test_零股數列跳過(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,0,100\n0050,17,96.76\n")
            self.assertEqual(source.read_csv_positions(path, COLUMNS),
                             {"0050": (17, 96.76)})

    def test_重複代號時報錯不靜默覆蓋(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,50,100\n2330,30,200\n")
            with self.assertRaisesRegex(RuntimeError, "重複"):
                source.read_csv_positions(path, COLUMNS)

    def test_小數股數報錯不截斷(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,100.5,100\n")
            with self.assertRaisesRegex(RuntimeError, "非負整數"):
                source.read_csv_positions(path, COLUMNS)

    def test_負股數報錯(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,-50,100\n")
            with self.assertRaisesRegex(RuntimeError, "非負整數"):
                source.read_csv_positions(path, COLUMNS)

    def test_NaN_與無限大報錯(self):
        for bad in ("nan", "inf", "-inf"):
            with tempfile.TemporaryDirectory() as d:
                path = write_csv(d, f"股票代號,股數,成交均價\n2330,{bad},100\n")
                with self.assertRaises(RuntimeError, msg=f"股數 {bad}"):
                    source.read_csv_positions(path, COLUMNS)
                path = write_csv(d, f"股票代號,股數,成交均價\n2330,50,{bad}\n", name="p2.csv")
                with self.assertRaises(RuntimeError, msg=f"均價 {bad}"):
                    source.read_csv_positions(path, COLUMNS)

    def test_缺少欄位時報錯(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數\n2330,50\n")
            with self.assertRaises(RuntimeError):
                source.read_csv_positions(path, COLUMNS)

    def test_數字壞掉時報錯不靜默(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n2330,abc,100\n")
            with self.assertRaises(RuntimeError):
                source.read_csv_positions(path, COLUMNS)

    def test_沒有任何有效列時報錯(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "股票代號,股數,成交均價\n")
            with self.assertRaises(RuntimeError):
                source.read_csv_positions(path, COLUMNS)


class TestSniffHeaders(unittest.TestCase):
    def test_回傳表頭(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, CSV_TEXT)
            self.assertEqual(source.sniff_headers(path), ["股票代號", "股數", "成交均價"])


class TestAutoMapColumns(unittest.TestCase):
    def test_常見中文表頭(self):
        m = source.auto_map_columns(["股票代號", "庫存股數", "成交均價"])
        self.assertEqual(m, {"code": "股票代號", "qty": "庫存股數", "price": "成交均價"})

    def test_英文表頭不分大小寫(self):
        m = source.auto_map_columns(["Code", "Qty", "Price"])
        self.assertEqual(m, {"code": "Code", "qty": "Qty", "price": "Price"})

    def test_認不出的欄位為_None(self):
        m = source.auto_map_columns(["名稱", "亂七八糟"])
        self.assertEqual(m, {"code": None, "qty": None, "price": None})

    def test_表頭含空白仍可對應(self):
        m = source.auto_map_columns([" 股票代號 ", "股數", "成交均價"])
        self.assertEqual(m["code"], " 股票代號 ")


class TestReadPositions(unittest.TestCase):
    def test_純_ARK_模式回傳_None(self):
        self.assertIsNone(source.read_positions({"source": "none"}))

    def test_csv_模式走檔案(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, CSV_TEXT)
            cfg = {"source": "csv", "csv": {"path": path, "columns": COLUMNS}}
            self.assertEqual(source.read_positions(cfg),
                             {"2330": (50, 2062.38), "0050": (17, 96.76)})

    def test_不明來源報錯(self):
        with self.assertRaises(RuntimeError):
            source.read_positions({"source": "quantum"})

    def test_未給_config_時讀預設路徑_不存在報_SetupRequired(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(source.SetupRequired):
                source.read_positions(config_path=os.path.join(d, "config.json"))

    def test_v2_空清單回_None(self):
        self.assertIsNone(source.read_positions({"version": 2, "accounts": []}))

    def test_v2_多檔案帳戶加權合併(self):
        with tempfile.TemporaryDirectory() as d:
            a = write_csv(d, "股票代號,股數,成交均價\n2330,1000,600\n", name="a.csv")
            b = write_csv(d, "股票代號,股數,成交均價\n2330,500,660\n", name="b.csv")
            cfg = {"version": 2, "accounts": [
                {"type": "file", "name": "甲", "path": a, "columns": COLUMNS},
                {"type": "file", "name": "乙", "path": b, "columns": COLUMNS}]}
            self.assertEqual(source.read_positions(cfg), {"2330": (1500, 620.0)})

    def test_檔案帳戶缺檔時報錯含帳戶名(self):
        cfg = {"version": 2, "accounts": [
            {"type": "file", "name": "國泰", "path": "/不存在/p.csv", "columns": COLUMNS}]}
        with self.assertRaisesRegex(RuntimeError, "國泰"):
            source.read_positions(cfg)


if __name__ == "__main__":
    unittest.main()
