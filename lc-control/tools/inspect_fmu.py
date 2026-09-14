"""读取 FMU 归档的 modelDescription.xml，检查变量与输入契约。
不加载 FMU 动态库，不推进热工，也不修改 Sustain-LC 仓库。"""
import argparse
import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fmu")
    args = parser.parse_args()
    with ZipFile(args.fmu) as archive:
        root = ET.fromstring(archive.read("modelDescription.xml"))
    declared_units = {}
    for item in root.findall("./TypeDefinitions/SimpleType"):
        declared_units[item.attrib["name"]] = list(item)[0].attrib.get("unit")
    inputs = []
    for variable in root.findall("./ModelVariables/ScalarVariable"):
        if variable.attrib.get("causality") != "input":
            continue
        value_type = list(variable)[0]
        inputs.append({"name": variable.attrib["name"], "causality": "input",
                       "type": value_type.tag,
                       "unit": value_type.attrib.get("unit") or declared_units.get(value_type.attrib.get("declaredType"))})
    print(json.dumps({"fmi_version": root.attrib["fmiVersion"], "inputs": inputs,
                      "count": len(inputs), "note": "Metadata only; runtime response remains unverified."},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

