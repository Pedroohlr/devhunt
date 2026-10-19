"""Exceções que precisam atravessar os try/except largos do motor de formulários."""
from __future__ import annotations


class ResumeBuildError(Exception):
    """Falha ao gerar o currículo (LLM fora do ar, sem créditos, validação).

    O currículo é gerado sob demanda, dentro do preenchimento do formulário; `forms.fill_field`
    e `flow` engolem exceções de propósito, então esta sobe sem ser capturada para o pipeline
    devolver o motivo certo e recolocar a vaga na fila.
    """
