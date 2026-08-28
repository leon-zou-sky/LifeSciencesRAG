"""
PII 对抗探针断言（冒烟级，与 test_answers.py 同定位）

对 config/pii_probes.json 逐条断言：
  - expect：每类应检出的实体必须检出（type+value 双匹配；只写 type 则只断言类型命中）
  - must_keep：脱敏后文本必须原样保留这些子串（误报防线——药品名/亲属称谓被动=失败）

探针集只增不改；规则升级后跑本脚本，通过数变少=退步。

用法:
    conda run -n py311 python scripts/test_pii.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pii_guard import detect, redact

_PROBES = Path(__file__).resolve().parent.parent / "config" / "pii_probes.json"


def main():
    probes = json.loads(_PROBES.read_text(encoding="utf-8"))["probes"]
    passed = failed = 0
    layer_stats = {}

    for p in probes:
        text = p["input"]
        redacted, entities = redact(text)
        errs = []

        # expect：应检出的实体
        for e in p["expect"]:
            found = [x for x in entities if x.type == e["type"]]
            if "value" in e:
                found = [x for x in found if x.value == e["value"]]
            if not found:
                want = f'{e["type"]}={e.get("value", "*")}'
                errs.append(f"漏检 {want}")

        # must_keep：误报防线
        for kw in p.get("must_keep", []):
            if kw not in redacted:
                errs.append(f"误伤 {kw!r}（脱敏后丢失）")

        ok = not errs
        passed += ok
        failed += not ok
        st = layer_stats.setdefault(p["layer"], [0, 0])
        st[0] += ok
        st[1] += 1

        icon = "✅" if ok else "❌"
        print(f"{icon} {p['id']} [{p['layer']}] {text[:38]}")
        if not ok:
            got = [(x.type, x.value) for x in entities]
            print(f"   检出: {got}")
            for e in errs:
                print(f"   ✗ {e}")
            print(f"   脱敏后: {redacted}")

    print("\n分层统计:")
    for layer, (ok, total) in sorted(layer_stats.items()):
        print(f"  {layer:16s} {ok}/{total}")
    print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{len(probes)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
