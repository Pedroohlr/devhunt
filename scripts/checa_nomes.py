"""Acha nome global usado e nunca definido no pacote (o erro que sumiu com o currículo).

`_resolve_resume` era chamado em dois lugares de forms.py e não existia em lugar nenhum: o
NameError caía no `except Exception` do `fill_field` e virava um discreto "✗ file", então 5
candidaturas foram enviadas sem currículo antes de alguém notar. import compila, então
compileall não pega; isto pega, sem dependência nenhuma.

Uso: .venv/bin/python scripts/checa_nomes.py   (silencioso = tudo certo, saída 1 = achou)
"""
from __future__ import annotations

import builtins
import pathlib
import symtable
import sys

RAIZ = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent / "src"
EMBUTIDOS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}


def definidos_no_modulo(tab: symtable.SymbolTable) -> set[str]:
    return {s.get_name() for s in tab.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_parameter()}


def varre(tab: symtable.SymbolTable, topo: set[str], achados: list[tuple[str, int]]) -> None:
    for s in tab.get_symbols():
        if s.is_global() and not s.is_assigned() and s.get_name() not in topo | EMBUTIDOS:
            achados.append((s.get_name(), tab.get_lineno()))
    for filho in tab.get_children():
        varre(filho, topo, achados)


def main() -> int:
    problemas = 0
    alvos = [RAIZ] if RAIZ.is_file() else sorted(RAIZ.rglob("*.py"))
    for py in alvos:
        src = py.read_text()
        try:
            topo_tab = symtable.symtable(src, str(py), "exec")
        except SyntaxError as e:
            print(f"{py}: erro de sintaxe: {e}")
            problemas += 1
            continue
        topo = definidos_no_modulo(topo_tab)
        achados: list[tuple[str, int]] = []
        varre(topo_tab, topo, achados)
        for nome, linha in sorted(set(achados)):
            print(f"{py.name}: nome global sem definição: {nome!r} (escopo na linha {linha})")
            problemas += 1
    if problemas:
        print(f"\n{problemas} nome(s) suspeito(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
