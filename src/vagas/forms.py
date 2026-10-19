"""Motor genérico de formulários: escaneia campos visíveis, preenche, acha o botão de avançar/enviar, detecta erros/sucesso."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

from playwright.sync_api import Frame, Locator, Page
from rapidfuzz import fuzz, process

from . import humanize as h
from .config import DATA_DIR
from .errors import ResumeBuildError
from .qa import FieldQ, best_option, _norm_opt

Scope = Union[Page, Frame]

DEEP_JS_HELPERS = r"""
  // atravessa shadow roots (abertos) — LinkedIn/SDUI, web components de ATS
  const deepQuery = (root, sel) => {
    const out = [];
    const walk = (node) => {
      if (!node || !node.querySelectorAll) return;
      node.querySelectorAll(sel).forEach(e => out.push(e));
      node.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
    };
    walk(root);
    return out;
  };
  const deepOne = (root, sel) => { const r = deepQuery(root, sel); return r.length ? r[0] : null; };
  const rootOf = (el) => { const r = el.getRootNode ? el.getRootNode() : document; return (r && r.querySelector) ? r : document; };
  const byId = (el, id) => { const r = rootOf(el); return (r.getElementById ? r.getElementById(id) : null) || document.getElementById(id); };
"""

SCAN_JS = r"""
(rootSel) => {
""" + DEEP_JS_HELPERS + r"""
  const qsRoot = (sel) => { try { return deepOne(document, sel); } catch (e) { return null; } };
  const isVisible = (el) => {
    if (!el) return false;
    const t0 = (el.getAttribute('type') || '').toLowerCase();
    if (t0 === 'file') return true;   // ATS escondem o input de arquivo (display:none) atrás de um botão; set_input_files funciona mesmo assim
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) {
      // inputs escondidos com label estilizado (radio/checkbox) ainda contam; input de largura/altura 0 (Vuetify) não
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (t === 'radio' || t === 'checkbox') {
        const lab = el.id ? (rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`) || document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) : el.closest('label');
        if (lab) { const lr = lab.getBoundingClientRect(); return lr.width > 0 && lr.height > 0; }
        const p = el.parentElement; if (p) { const pr = p.getBoundingClientRect(); return pr.width > 0 && pr.height > 0; }
      }
      return false;
    }
    return true;
  };
  const textOf = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const CONTROL_SEL = 'input, textarea, select, [role="combobox"], [contenteditable="true"], [contenteditable="plaintext-only"], [contenteditable=""], [role="textbox"], button[aria-haspopup="listbox"], button[aria-haspopup="menu"], button[aria-haspopup="true"], button[role="checkbox"], button[role="switch"]';

  // formulário "uma pergunta por tela" (Quill Forms / InHire form-app): blocos anteriores e futuros ficam no DOM
  // fora da tela; só o bloco ativo importa (opções, botão OK, erros)
  const ACTIVE_BLOCK_SEL = '.renderer-components-field-wrapper.active, [class*="field-wrapper" i][class*="active" i], [class*="block-wrapper" i][class*="active" i]';
  const activeBlock = () => { const a = deepQuery(document, ACTIVE_BLOCK_SEL).filter(isVisible); return a.length === 1 ? a[0] : null; };
  const openDialog = () => { const ds = deepQuery(document, '[aria-modal="true"], .MuiDialog-container, .modal.show, .artdeco-modal').filter(d => isVisible(d) && !d.closest('[aria-hidden="true"]') && !d.querySelector('[role="menu"], [role="listbox"]') && !/cookie|consent|lgpd/i.test((d.id || '') + ' ' + (d.className || '') + ' ' + (d.innerText || '').slice(0, 300))); return ds.length ? ds[ds.length - 1] : null; };
  let root = rootSel ? qsRoot(rootSel) : (openDialog() || activeBlock());
  if (!root) {
    // escolhe o <form> visível com mais controles; senão o documento
    const forms = deepQuery(document, 'form').filter(isVisible);
    let best = null, bestN = 0;
    for (const f of forms) { const n = deepQuery(f, CONTROL_SEL).length; if (n > bestN) { best = f; bestN = n; } }
    root = (best && bestN >= 1) ? best : document.body;
  }

  const controls = deepQuery(root, CONTROL_SEL).filter(el => {
    const role = el.getAttribute('role') || '';
    const t = (el.getAttribute('type') || '').toLowerCase();
    // <button type="button" role="checkbox"> (Radix/shadcn) é campo, apesar do type=button
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(t) && !['checkbox', 'switch', 'combobox'].includes(role)) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    // campo de busca DENTRO de um dropdown/popup aberto (Indeed "Pesquise para selecionar", react-dropdown-select) não é pergunta
    const ph = (el.getAttribute('placeholder') || '');
    if (/pesquis|busca|search|filtr/i.test(ph) && el.closest('[role="dialog"], [role="listbox"], [role="menu"], [class*="dropdown" i], [class*="popup" i], [class*="popover" i]')) return false;
    // busca de vagas do site de carreiras ("Search for jobs by title", "search job by location") não é campo de candidatura
    const al = ((el.getAttribute('aria-label') || '') + ' ' + ph + ' ' + (el.getAttribute('name') || '') + ' ' + (el.id || '')).toLowerCase();
    // widget de alerta de vagas (SuccessFactors: "frequência (em dias) de recebimento de alertas") também não
    if (/alert|frequ[eê]ncia|frequency/.test(al) || el.closest('[class*="alert" i][class*="job" i], [id*="jobalert" i], [id*="job-alert" i], form[id*="alert" i]')) return false;
    // FILTROS DE BUSCA DE VAGAS do site da empresa/ATS (abler: "Cargo ou palavras-chave", "Cidade", "Mobilidade" + botão
    // "Pesquisar vagas"): o bloco que contém um botão Pesquisar/Buscar e nenhum botão de candidatar/enviar não é formulário
    {
      const box = el.closest('form, [role="search"], [class*="search" i], [class*="filter" i], [class*="filtro" i], [class*="busca" i], section, div');
      let node = box;
      for (let k = 0; k < 7 && node && node !== document.body; k++, node = node.parentElement) {
        const txt = (b) => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
        // só botões com texto (ícones/setas de dropdown não decidem nada)
        const btns = Array.from(node.querySelectorAll('button, [role="button"], input[type="submit"], a.btn')).filter(b => txt(b).length >= 3 && !(b.contains(el)));
        if (!btns.length) continue;
        const hasSearch = btns.some(b => /^(pesquisar|buscar|search|filtrar|procurar)( vagas| jobs| oportunidades| emprego| agora)?\s*$/i.test(txt(b)));
        const hasApply = btns.some(b => /candidat|apply|inscrev|enviar|submit|avan[cç]ar|continuar|next|finalizar|salvar|prosseguir/i.test(txt(b)));
        if (hasSearch && !hasApply) return false;
        break;   // achou o bloco de botões com texto mais próximo: decide por ele
      }
    }
    if (/(search|pesquis|busca|procur).{0,30}(job|vaga|keyword|palavra|location|local|cargo|title)|(job|vaga).{0,20}(search|pesquis|busca)|keyword|typeahead-job/.test(al)) return false;
    if (el.closest('[role="search"], form[role="search"], [class*="job-search" i], [class*="jobsearch" i], [id*="search" i][class*="job" i]')) return false;
    return isVisible(el);
  });

  let n = 0;
  const mark = (el) => { if (!el.getAttribute('data-vagas-id')) el.setAttribute('data-vagas-id', 'v' + (++n) + '_' + Date.now().toString(36)); return el.getAttribute('data-vagas-id'); };

  const ownLabel = (el) => {
    let t = '';
    if (el.id) { const l = rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`) || document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = textOf(l); }
    if (!t) { const l = el.closest('label'); if (l && l.querySelectorAll('input,select,textarea').length <= 1) { const c = l.cloneNode(true); c.querySelectorAll('input,select,textarea').forEach(x => x.remove()); t = textOf(c); } }
    if (!t && el.getAttribute('aria-labelledby')) t = el.getAttribute('aria-labelledby').split(/\s+/).map(id => textOf(byId(el, id))).join(' ').trim();
    if (!t && el.getAttribute('aria-label')) t = el.getAttribute('aria-label');
    return t;
  };
  const PLACEHOLDER_RE = /^(selecione|select|escolha|choose|informe|digite|pesquis|busca|search)/i;
  const containerLabel = (el, stopAt) => {
    let p = el.parentElement;
    let fallback = '';
    for (let i = 0; i < 6 && p && p !== root && p !== stopAt; i++, p = p.parentElement) {
      // 1) rótulos de verdade primeiro (label/legend/classes de label ou pergunta)
      for (const c of p.querySelectorAll('label, legend, [class*="label" i], [class*="question" i], [class*="title" i], h2, h3, h4, h5')) {
        if (c.contains(el) || c.querySelector('input,select,textarea')) continue;
        const t = textOf(c);
        if (t && t.length >= 3 && t.length <= 300 && !PLACEHOLDER_RE.test(t)) return t;
      }
      // 2) texto genérico (p/span/div) só como reserva, e nunca um placeholder
      if (!fallback) for (const c of p.querySelectorAll('p, span, div')) {
        if (c.contains(el) || c.querySelector('input,select,textarea')) continue;
        const t = textOf(c);
        if (t && t.length >= 3 && t.length <= 300 && !PLACEHOLDER_RE.test(t)) { fallback = t; break; }
      }
    }
    return fallback;
  };
  const prevHeading = (el) => {
    // <h3>Nome completo *</h3><p>dica</p><input> (Breezy): título anterior mais próximo; títulos valem mais que dicas (p/span)
    let heading = '', hint = '';
    let sib = el.previousElementSibling;
    for (let k = 0; k < 4 && sib; k++, sib = sib.previousElementSibling) {
      if (sib.matches('input, select, textarea, button') || sib.querySelector('input, select, textarea')) break;
      const t = textOf(sib);
      if (!t || t.length < 2 || t.length > 300 || PLACEHOLDER_RE.test(t)) continue;
      const isHint = /^(por exemplo|ex\.?:|exemplo|e\.g\.|\(|obs\.?:|dica)/i.test(t) || sib.matches('[class*="body" i], [class*="hint" i], [class*="help" i], [class*="desc" i], [class*="example" i]');
      if (isHint) continue;   // "Por exemplo: Fortaleza - Ceará" / div.question-body não é a pergunta
      if (sib.matches('h1, h2, h3, h4, h5, h6, label, legend, [class*="label" i], [class*="question" i], [class*="title" i]')) { heading = t; break; }
      if (!hint && sib.matches('p, span, strong, b, div')) hint = t;
    }
    return heading || hint;
  };
  const labelOf = (el) => {
    let t = ownLabel(el);
    const ph0 = (el.getAttribute('placeholder') || '').trim();
    if (!t) t = prevHeading(el);
    if (!t) t = containerLabel(el);
    // título da página virando rótulo de TODOS os campos (DBC/Angular): o placeholder é o rótulo real
    if (ph0 && t && t.length > 45 && ph0.length <= 40) t = ph0;
    if (!t) t = ph0;
    if (!t) t = el.getAttribute('name') || el.id || '';
    t = t.replace(/\s*\*\s*$/, '').trim();
    t = t.replace(/^\d{1,2}\s+(?=[A-Za-zÀ-ú¿¡(])/, '');   // "3 Se outra, informe" (contador do Quill Forms) -> "Se outra, informe"
    // "E-mail E-mail" / "E-mail E-mail Obrigatório" -> "E-mail"
    const half = t.slice(0, Math.floor(t.length / 2)).trim();
    if (half && t.startsWith(half + ' ' + half)) t = t.slice(half.length).trim();
    t = t.replace(/\s+(Obrigat[oó]rio|Required)$/i, '');
    return t.slice(0, 300);
  };
  const groupLabel = (els) => {
    const first = els[0];
    const fs = first.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && textOf(lg)) return textOf(lg); if (fs.getAttribute('aria-label')) return fs.getAttribute('aria-label'); }
    const rg = first.closest('[role="radiogroup"], [role="group"]');
    if (rg) {
      if (rg.getAttribute('aria-label')) return rg.getAttribute('aria-label');
      if (rg.getAttribute('aria-labelledby')) return rg.getAttribute('aria-labelledby').split(/\s+/).map(id => textOf(byId(rg, id))).join(' ').trim();
    }
    // ancestral comum -> label/p anterior
    let anc = first.parentElement;
    while (anc && anc !== root && !els.every(e => anc.contains(e))) anc = anc.parentElement;
    if (anc) {
      const optionLabels = new Set(els.map(e => optLabel(e)));
      // pergunta como irmão anterior do contêiner das opções (<p>Você já trabalha aqui?</p><div>...Sim/Não...</div>)
      let node = anc;
      for (let lvl = 0; lvl < 3 && node && node !== root; lvl++, node = node.parentElement) {
        let sib = node.previousElementSibling;
        for (let k = 0; k < 3 && sib; k++, sib = sib.previousElementSibling) {
          if (sib.querySelector('input,select,textarea,button')) break;
          const t = textOf(sib);
          if (t && t.length >= 3 && t.length <= 300 && !optionLabels.has(t) && !PLACEHOLDER_RE.test(t) && !/^(opcional|obrigat[oó]rio|required|optional)$/i.test(t)) return t;
          if (t) break;
        }
      }
      const cands = anc.querySelectorAll('label, legend, p, span, div, h3, h4, h5, [class*="label" i], [class*="question" i]');
      for (const c of cands) {
        if (c.querySelector('input,select,textarea')) continue;
        const t = textOf(c);
        if (t && t.length >= 3 && t.length <= 300 && !optionLabels.has(t)) return t;
      }
      const cl = containerLabel(anc, root); if (cl) return cl;
    }
    return first.getAttribute('name') || '';
  };
  const optLabel = (e) => {
    let t = ownLabel(e);
    if (t) return t;
    let sib = e.nextElementSibling;
    if (sib && !sib.matches('input, select, textarea') && textOf(sib)) return textOf(sib);   // <input><span>Opção</span> (Breezy)
    const par = e.parentElement;
    if (par && par.matches('li, div, td, p')) { const c = par.cloneNode(true); c.querySelectorAll('input,select,textarea').forEach(x => x.remove()); const pt = textOf(c); if (pt && pt.length <= 200) return pt; }
    return e.value || '';
  };
  const isRequired = (el) => el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(ownLabel(el)) || !!(el.closest('[class*="required" i]')) || (!ownLabel(el) && /\*\s*$/.test(containerLabel(el)));

  const out = [];
  const radioGroups = {};
  const checkGroups = {};
  // checkboxes com nome indexado ("checkbox-4329641-0/1/2", "skills[0]"...) são um grupo (Gupy/MUI):
  const cbPrefix = (el) => (el.name || '').replace(/[-_.]?\d+\]?$|\[\d*\]$/, '');
  const prefixCount = {};
  for (const el of controls) { if ((el.getAttribute('type') || '').toLowerCase() === 'checkbox') { const pf = cbPrefix(el); if (pf && pf !== el.name) prefixCount[pf] = (prefixCount[pf] || 0) + 1; } }
  const checkboxKey = (el) => {
    const pf = cbPrefix(el);
    if (pf && pf !== el.name && prefixCount[pf] >= 2) return 'pfx_' + pf;
    return el.name || 'c_' + (el.closest('fieldset,[role=group]')?.id || mark(el));
  };
  const done = new Set();
  for (let el of controls) {
    // Vuetify/Angular Material: <div role="combobox"> que envolve um <input> real -> o campo é o input (dá para digitar)
    if (el.tagName !== 'INPUT' && el.getAttribute('role') === 'combobox') {
      const inner = el.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])');
      if (inner && !inner.readOnly && !inner.disabled && isVisible(inner) && inner.getBoundingClientRect().width > 5) el = inner;   // input de largura 0 (Vuetify v-select): fica com o div clicável
    }
    if (done.has(el)) continue;
    done.add(el);
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' && (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch')) {
      // shadcn/Radix: <button role="checkbox" aria-checked="false"> com <label for=...> ao lado
      const id = mark(el);
      out.push({ id, tag, type: 'checkbox', kind: 'checkbox', label: labelOf(el), name: el.getAttribute('name') || '',
                 required: isRequired(el), value: (el.getAttribute('aria-checked') === 'true' || el.getAttribute('data-state') === 'checked') ? 'true' : '',
                 options: [], optionIds: {} });
      continue;
    }
    if (tag === 'button') {
      // dropdown em <button> ("Selecione uma opção", ilegra/Headless UI): combobox com o texto atual como valor.
      // Menus de navegação/usuário (Gupy: avatar "Fulano · Editar perfil · Sair") não são campo.
      if (el.closest('nav, header, footer, [role="navigation"], [role="menubar"], [class*="menu" i], [class*="avatar" i], [class*="user" i], [class*="navbar" i], [class*="header" i]')) continue;
      const bt = textOf(el);
      if (bt.length > 60 || /\b(sair|logout|perfil|minha conta|minhas candidaturas)\b/i.test(bt)) continue;
      // botão de AÇÃO com menu (SuccessFactors "Candidatar-se agora ▾", "Compartilhar ▾") não é campo de formulário
      if (/candidat|apply|inscrev|enviar|iniciar|compartilh|share|entrar|login|idioma|language|salvar|save/i.test(bt)) continue;
      const id = mark(el);
      out.push({ id, tag, type: 'button', label: labelOf(el), name: el.getAttribute('name') || '', required: isRequired(el), value: PLACEHOLDER_RE.test(bt) ? '' : bt, options: [], optionIds: {}, kind: 'combobox' });
      continue;
    }
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (type === 'radio') { const k = el.name || ('r_' + (el.closest('fieldset,[role=radiogroup]')?.id || 'x')); (radioGroups[k] = radioGroups[k] || []).push(el); continue; }
    if (type === 'checkbox') { const k = checkboxKey(el); (checkGroups[k] = checkGroups[k] || []).push(el); continue; }
    const id = mark(el);
    const rec = { id, tag, type, label: labelOf(el), name: el.name || '', required: isRequired(el), value: '', options: [], optionIds: {},
                  placeholder: (el.getAttribute('placeholder') || '').trim() };
    // widget "alerta de vagas" pelo rótulo (SuccessFactors: "Selecione a frequência (em dias) de recebimento de alertas")
    if (/frequ[eê]ncia.{0,40}(dias|alert)|alerta(s)? de vaga|job alert|receber (vagas|alertas|novas oportunidades)|criar alerta/i.test(rec.label || '')) continue;
    if (tag === 'select') {
      rec.kind = 'select';
      rec.options = Array.from(el.options).map(o => textOf(o)).filter(t => t && !/^(selecione|select|escolha|choose|-+|\.\.\.|selecionar)/i.test(t));
      rec.value = el.selectedIndex > 0 ? textOf(el.options[el.selectedIndex]) : '';
      // período da pretensão salarial (Yearly/Monthly) vem "Yearly" por padrão: sempre reconsiderar
      if (/^(yearly|annual|anual)$/i.test(rec.value) && rec.options.some(o => /monthly|mensal/i.test(o))) rec.value = '';
    } else if (tag === 'textarea' || (el.hasAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false') || el.getAttribute('role') === 'textbox') {
      // Notion Forms: <div contenteditable="plaintext-only" placeholder="Sua resposta"> -> campo de texto; o placeholder não é valor
      rec.kind = 'textarea'; rec.value = (el.value !== undefined ? el.value : textOf(el)) || '';
      const phc = el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '';
      if (phc && rec.value === phc.trim()) rec.value = '';
      if (tag !== 'textarea' && rec.label && rec.label.length <= 40 && !/\n/.test(rec.label)) rec.kind = 'text';   // uma linha (nome, telefone) digita como texto
    } else if (type === 'file') {
      rec.kind = 'file'; rec.value = el.files && el.files.length ? 'has_file' : '';
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') || el.closest('[class*="select__control" i], [class*="autocomplete" i], .MuiAutocomplete-root, [class*="dropdown-select" i], [class*="react-select" i], [class*="Select-control" i], [role="combobox"], [role="listbox"], .v-select, .v-autocomplete, [class*="v-input__slot" i]')) {
      rec.kind = 'combobox'; rec.value = el.value || '';
      if (/multi[-_ ]?select/i.test(el.id || '') || el.getAttribute('aria-multiselectable') === 'true' || /multi[-_ ]?select/i.test(el.getAttribute('data-testid') || '')) rec.kind = 'multiselect';   // Indeed: lista com várias marcações
      if (el.getAttribute('list')) { const dl = byId(el, el.getAttribute('list')); if (dl) rec.options = Array.from(dl.options).map(o => o.value || textOf(o)); }
      // valor já escolhido em react-select
      const ctl = el.closest('[class*="select__control" i], [class*="control" i]');
      if (ctl && !rec.value) { const sv = ctl.querySelector('[class*="single-value" i], [class*="singleValue" i], [class*="multi-value" i]'); if (sv) rec.value = textOf(sv); }
    } else {
      rec.kind = ['email', 'tel', 'url', 'number', 'date', 'month', 'password'].includes(type) ? type : 'text';
      rec.value = el.value || '';
      // "Selecione o país" e afins: input de texto que abre uma lista -> combobox
      const ph = (el.getAttribute('placeholder') || '') + ' ' + rec.label;
      if (rec.kind === 'text' && (/^\s*(selecione|select|escolha|choose)\b/i.test(ph.trim()) || el.getAttribute('aria-haspopup') || el.getAttribute('aria-expanded') !== null)) rec.kind = 'combobox';
      if (rec.kind === 'tel' && /^\+?\d{1,3}\s*$/.test(rec.value)) { rec.prefix = rec.value; rec.value = ''; }   // só o +55: vazio
      if (rec.kind === 'tel' && /^\d{3}\.\d{3}\.\d{3}-\d{2}$/.test(rec.value.trim())) rec.value = '';   // CPF herdado de rascunho (Breezy) no campo de telefone
    }
    out.push(rec);
  }
  for (const [k, els] of Object.entries(radioGroups)) {
    const ids = {}; const opts = [];
    let checked = '';
    for (const e of els) { const oid = mark(e); const lab = optLabel(e); ids[lab] = oid; opts.push(lab); if (e.checked) checked = lab; }
    const gl = groupLabel(els);
    out.push({ id: 'g_' + mark(els[0]), tag: 'input', type: 'radio', kind: 'radio', label: gl.replace(/\s*\*\s*$/, '').trim(), name: k, required: els.some(isRequired) || /\*\s*$/.test(gl), value: checked, options: opts, optionIds: ids });
  }
  for (const [k, els] of Object.entries(checkGroups)) {
    if (els.length === 1) {
      const e = els[0]; const oid = mark(e);
      out.push({ id: oid, tag: 'input', type: 'checkbox', kind: 'checkbox', label: labelOf(e), name: k, required: isRequired(e), value: e.checked ? 'true' : '', options: [], optionIds: {} });
    } else {
      const ids = {}; const opts = []; const checked = [];
      for (const e of els) { const oid = mark(e); const lab = optLabel(e); ids[lab] = oid; opts.push(lab); if (e.checked) checked.push(lab); }
      const gl = groupLabel(els);
      out.push({ id: 'g_' + mark(els[0]), tag: 'input', type: 'checkbox', kind: 'multiselect', label: gl.replace(/\s*\*\s*$/, '').trim(), name: k, required: els.some(isRequired) || /\*\s*$/.test(gl), value: checked.join('; '), options: opts, optionIds: ids });
    }
  }
  // grupos de opções customizados (Quill Forms/InHire "renderer-core", Typeform-like): divs clicáveis sem <input>
  const CHOICE_SEL = '[class*="optionWrapper" i], [class*="choiceWrapper" i], [class*="choice-wrapper" i], [role="radio"]:not(input), [role="checkbox"]:not(input)';
  const BLOCK_SEL = 'section[id^="block-"], [class*="block-stack" i], [class*="field-wrapper" i], [class*="question" i], fieldset, [role="radiogroup"], [role="group"]';
  const choiceEls = deepQuery(root, CHOICE_SEL).filter(el => isVisible(el) && !el.querySelector('input,select,textarea') && !el.closest('[aria-hidden="true"]') && !el.closest('label'));
  const blocks = new Map();
  for (const el of choiceEls) {
    const blk = el.closest(BLOCK_SEL) || el.parentElement.parentElement || el.parentElement;
    if (!blocks.has(blk)) blocks.set(blk, []);
    blocks.get(blk).push(el);
  }
  for (const [blk, els] of blocks.entries()) {
    if (els.length < 2) continue;
    const optText = (el) => {
      const lab = el.querySelector('[class*="optionLabel" i], [class*="choiceLabel" i], [class*="label" i]');
      let t = textOf(lab) || textOf(el);
      // "Financeiro A" (tecla de atalho no fim) -> "Financeiro"
      if (!lab) t = t.replace(/\s+[A-Z]$/, '');
      return t;
    };
    const optLabels = els.map(optText);
    let label = '';
    const HINT_JS_RE = /pressione (enter|shift)|press (enter|shift)|↵|^\d+\s*%/i;
    for (const c of blk.querySelectorAll('[class*="block-label" i], [class*="question-title" i], [class*="question" i] p, legend, h2, h3, h4, h5, label, p')) {
      if (els.some(e => c.contains(e) || e.contains(c))) continue;
      if (c.closest('[class*="helper" i], [class*="hint" i], [class*="footer" i]')) continue;
      const t = textOf(c);
      if (t && t.length >= 3 && t.length <= 1200 && !optLabels.includes(t) && !PLACEHOLDER_RE.test(t) && !HINT_JS_RE.test(t)) { label = t; break; }
    }
    if (!label) { label = blk.getAttribute('aria-label') || ''; }
    const required = /\*\s*$/.test(label) || blk.getAttribute('aria-required') === 'true' || !!blk.querySelector('[class*="required" i]') || /\bobrigat[oó]rio\b/i.test(textOf(blk).slice(-200));
    label = label.replace(/\s*\*\s*$/, '').trim();
    const ids = {}; const checked = [];
    els.forEach((e, i) => {
      const oid = mark(e); const lab = optLabels[i]; ids[lab] = oid;
      const sel = e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true' || /(^|\s|__)(selected|checked|is-selected|is-checked)(\s|$|--)/i.test(e.className || '');
      if (sel) checked.push(lab);
    });
    // "multipleChoice" no Quill Forms é o bloco de escolha ÚNICA; só é múltipla com aria-multiselectable/role=checkbox
    const multi = blk.getAttribute('aria-multiselectable') === 'true' || els.some(e => e.getAttribute('role') === 'checkbox');
    out.push({ id: 'g_' + mark(els[0]), tag: 'div', type: multi ? 'checkbox' : 'radio', kind: multi ? 'multiselect' : 'radio', label: label.slice(0, 300), name: blk.id || '', required, value: checked.join('; '), options: optLabels, optionIds: ids });
  }
  // grupos de <button> curtos e irmãos ("Sim"/"Não" na ilegra, Pipefy, Typeform-like) = radio sem <input>
  const BTN_SKIP = /^(avan[cç]ar|pr[oó]xim|continuar|enviar|submit|cancelar|voltar|fechar|remover|salvar|ok$|anexar|upload|selecion|carregar|adicionar|entrar|login|buscar|pesquisar|filtrar|ver |mostrar|abrir|copiar|compartilhar)/i;
  const already = new Set(); for (const r of out) for (const oid of Object.values(r.optionIds || {})) already.add(oid);
  const bcands = deepQuery(root, 'button:not([type="submit"])').filter(b => isVisible(b) && !b.closest('nav, header, footer, [aria-hidden="true"], [role="dialog"] [role="menu"]') && !b.querySelector('input,select,textarea') && !b.getAttribute('data-vagas-id') && !b.getAttribute('aria-haspopup'));
  const bgroups = new Map();
  for (const b of bcands) {
    const t = textOf(b);
    if (!t || t.length > 40 || BTN_SKIP.test(t)) continue;
    const par = b.parentElement; if (!par) continue;
    if (!bgroups.has(par)) bgroups.set(par, []);
    bgroups.get(par).push(b);
  }
  for (const [par, els] of bgroups.entries()) {
    if (els.length < 2 || els.length > 12) continue;
    // todos os filhos-botão do contêiner devem ser curtos (senão é uma barra de ações, não uma pergunta)
    if (Array.from(par.children).some(c => c.tagName === 'BUTTON' && !els.includes(c))) continue;
    const optLabels = els.map(textOf);
    let label = '';
    let p = par.parentElement;
    for (let i = 0; i < 5 && p && p !== root && !label; i++, p = p.parentElement) {
      for (const c of p.querySelectorAll('label, legend, [class*="label" i], [class*="question" i], [class*="title" i], h2, h3, h4, h5, p, span')) {
        if (c.contains(par) || c.querySelector('input,select,textarea,button')) continue;
        const t = textOf(c);
        if (t && t.length >= 3 && t.length <= 300 && !optLabels.includes(t) && !PLACEHOLDER_RE.test(t) && !/^(opcional|obrigat[oó]rio|required|optional)$/i.test(t)) { label = t; break; }
      }
    }
    if (!label) continue;   // botões soltos sem pergunta (chips de tags no cabeçalho) não são campo
    const ids = {}; const checked = [];
    els.forEach((e, i) => {
      const oid = mark(e); ids[optLabels[i]] = oid;
      if (e.getAttribute('aria-pressed') === 'true' || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true' || /(^|\s)(selected|active|checked|is-selected|is-active)(\s|$)/i.test(e.className || '')) checked.push(optLabels[i]);
    });
    out.push({ id: 'g_' + mark(els[0]), tag: 'button', type: 'radio', kind: 'radio', label: label.replace(/\s*\*\s*$/, '').trim().slice(0, 300), name: '', required: /\*\s*$/.test(label), value: checked.join('; '), options: optLabels, optionIds: ids });
  }
  // ordem visual
  const order = (rec) => { const el = deepOne(document, `[data-vagas-id="${(rec.optionIds && Object.values(rec.optionIds)[0]) || rec.id}"]`); const r = el ? el.getBoundingClientRect() : { top: 1e9, left: 0 }; return r.top * 10000 + r.left; };
  out.sort((a, b) => order(a) - order(b));
  return out;
}
"""

BUTTONS_JS = r"""
(rootSel) => {
""" + DEEP_JS_HELPERS + r"""
  const textOf = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => { const st = getComputedStyle(el); if (st.display === 'none' || st.visibility === 'hidden') return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const active = deepQuery(document, '.renderer-components-field-wrapper.active, [class*="field-wrapper" i][class*="active" i], [class*="block-wrapper" i][class*="active" i]').filter(isVisible);
  // diálogo/modal aberto (MUI Dialog na Gupy: "as respostas não poderão ser editadas" -> Salvar e continuar) tem
  // prioridade: o resto da página fica aria-hidden e seus botões não devem ser clicados
  const dialogs = deepQuery(document, '[aria-modal="true"], .MuiDialog-container, .modal.show, .modal[style*="display: block"], .artdeco-modal').filter(d => isVisible(d) && !d.closest('[aria-hidden="true"]') && !d.querySelector('[role="menu"], [role="listbox"], [role="combobox"] + [role="dialog"]') && !/cookie|consent|lgpd/i.test((d.id || '') + ' ' + (d.className || '') + ' ' + (d.innerText || '').slice(0, 300)));
  // só vale como raiz o diálogo que realmente tem controles dentro
  const comControles = dialogs.filter(d => d.querySelector('button, input, select, textarea, [role="button"]'));
  const dialog = comControles.length ? comControles[comControles.length - 1] : null;
  const root = (rootSel && deepOne(document, rootSel)) || dialog || (active.length === 1 ? active[0] : null) || document.body;
  const SEL_BTN = 'button, input[type="submit"], a[role="button"], [role="button"], a.btn, a[class*="button" i]';
  const coletar = (r) => deepQuery(r, SEL_BTN).filter(e => isVisible(e) && !e.closest('[aria-hidden="true"]'));
  let els = coletar(root);
  // diálogo VAZIO não pode sequestrar a busca: o <po-modal> do TOTVS carrega o formulário de outra
  // origem e fica sem controles, e o motor passava a não enxergar "Enviar candidatura" nenhuma.
  if (!els.length && root !== document.body) els = coletar(document.body);
  let n = 0;
  return els.map(e => {
    if (!e.getAttribute('data-vagas-btn')) e.setAttribute('data-vagas-btn', 'b' + (++n) + '_' + Date.now().toString(36));
    const r = e.getBoundingClientRect();
    return { id: e.getAttribute('data-vagas-btn'), text: (e.tagName === 'INPUT' ? e.value : textOf(e)).slice(0, 80), aria: e.getAttribute('aria-label') || '', type: (e.getAttribute('type') || '').toLowerCase(), top: r.top, left: r.left, disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true') };
  });
}
"""

SUBMIT_RE = re.compile(
    r"^(enviar( (a |o |meu |minha |sua |a minha |a sua )?(candidatura|inscri[cç][aã]o|cadastro|aplica[cç][aã]o|formul[aá]rio|dados|curr[ií]culo|cv))?|submit( (my |your )?application)?|finalizar( candidatura| inscri[cç][aã]o| cadastro)?|"
    r"concluir( candidatura| inscri[cç][aã]o)?|confirmar( candidatura| inscri[cç][aã]o)?|"
    r"candidatar(-se)?( agora| [àa] vaga| a esta vaga| para (a|esta) vaga)?|apply( now| for this job)?|send( application)?|finish|"
    r"inscrever-se|quero me candidatar|enviar minha inscri[cç][aã]o|enviar (assim|mesmo) (mesmo|assim)|submit anyway|"
    r"salvar e enviar|enviar respostas|enviar e concluir|submeter( candidatura| inscri[cç][aã]o)?)$",
    re.I,
)
NEXT_RE = re.compile(
    r"^(avan[cç]ar|pr[oó]xim[oa]( etapa| passo)?|continuar( inscri[cç][aã]o| candidatura| cadastro)?|next( step)?|continue( to next step)?|"
    r"revisar( candidatura| sua candidatura)?|review( your application)?|salvar e continuar|save and continue|prosseguir|seguir|ok|confirmar|confirm|"
    r"verificar (a |sua |minha )?(candidatura|inscri[cç][aã]o)|check (your )?application|"
    r"iniciar|come[cç]ar|start|begin|responder( agora| (as )?perguntas| question[aá]rio)?|answer( now)?|vamos l[aá]|"
    r"continuar (assim|mesmo) (mesmo|assim)|continue anyway|sim, continuar|prosseguir (assim|mesmo) (mesmo|assim))$",
    re.I,
)
# candidatura recebida, mas o ATS pede confirmação por e-mail (GeekHunter: "Você precisa confirmar sua candidatura pelo e-mail")
CONFIRM_EMAIL_RE = re.compile(
    r"confirmar (sua|a) candidatura pelo e-?mail|confirme (sua candidatura|seu e-?mail|o e-?mail)|verifique (seu|o) e-?mail|"
    r"enviamos um (e-?mail|link) (de confirma|para confirma|para valida)|check your (e-?mail|inbox) to confirm|confirm your (application|email)",
    re.I,
)
BAD_RE = re.compile(r"voltar|back|cancel|sair|descartar|discard|fechar|close|remover|remove|excluir|delete|editar|edit|login|entrar|sign in|cadastr|"
                    r"^\s*(buscar|pesquisar|search|filtrar|procurar)\b|achar vagas|find jobs|encontrar vagas|assinar|subscribe|newsletter|compartilh|share", re.I)

SUCCESS_RE = re.compile(
    r"candidatura (foi )?(enviada|realizada|conclu[ií]da|recebida|efetuada|finalizada|registrada|feita|submetida)|candidatura submetida|application (sent|submitted|received|complete)|"
    r"acompanhar (sua )?candidatura|vai analisar sua compatibilidade|"
    r"obrigad[oa] por (se candidatar|sua candidatura)|thank(s| you) for applying|you('ve| have) applied|applied successfully|"
    r"inscri[cç][aã]o (realizada|conclu[ií]da|feita|enviada|efetuada)( com sucesso)?|recebemos sua candidatura|sua candidatura est[aá] (registrada|confirmada)|"
    r"parab[eé]ns.{0,40}candidat|successfully applied|your application has been|candidatura confirmada|"
    r"(suas )?respostas (foram )?(enviadas|registradas|recebidas)|obrigad[oa] (pelas|por suas) respostas|formul[aá]rio (foi )?enviado|"
    r"thanks for (filling|completing)|recebemos suas respostas|question[aá]rio (enviado|conclu[ií]do)",
    re.I,
)
ALREADY_RE = re.compile(
    r"voc[eê] j[aá] se candidatou|already applied|candidatura j[aá] (enviada|realizada)|you have already applied|j[aá] est[aá] participando|"
    r"you('ve| have) already submitted|j[aá] se inscreveu|candidatura enviada h[aá]|applied \d+ (minutes?|hours?|days?) ago",
    re.I,
)
LIMIT_RE = re.compile(r"exceeded the daily application limit|daily application limit|limite di[aá]rio de candidaturas|reached today's easy apply limit", re.I)
# exige conta para candidatar (NOUS: "Access restricted. To apply for this role, you need to sign up")
ACCOUNT_WALL_RE = re.compile(
    r"access restricted|to apply.{0,60}(sign ?up|log ?in|create an account|an account)|(crie|criar|fa[cç]a) (uma |sua |o )?(conta|cadastro|login).{0,40}(candidat|inscri|aplicar)|"
    r"(fa[cç]a login|entre|cadastre-se) para (se )?(candidatar|inscrever|aplicar)|voc[eê] precisa (estar logado|fazer login|ter uma conta)|"
    r"fazer login com (google|e-?mail|linkedin|apple)|sign in with (google|email|linkedin|apple)|entrar com (google|e-?mail|linkedin)|log in with (google|email)",
    re.I,
)
# candidatura enviada mas reprovada na hora por pergunta eliminatória (Gupy: URL .../reproved; "infelizmente... não atende")
REJECTED_RE = re.compile(r"/reproved\b|reprovad[oa]|n[aã]o atende (aos|os) (requisitos|pr[eé]-requisitos)|infelizmente.{0,80}(perfil|requisitos|etapa)|n[aã]o segue (para|no) processo|not selected to move forward", re.I)
# vaga encerrada/expirada (LinkedIn: "Não aceita mais candidaturas"; ATS: "vaga encerrada")
CLOSED_RE = re.compile(r"n[aã]o aceita mais candidaturas|no longer accepting applications|not currently accepting applications|n[aã]o est[aá] (mais )?aceitando candidaturas|vaga (encerrada|expirada|finalizada|fechada|n[aã]o (encontrada|est[aá] mais dispon[ií]vel))|this job is no longer available|job (has )?(closed|expired)|inscri[cç][oõ]es encerradas|"
                       r"/candidates/not-found|/not-found\b|/404\b|p[aá]gina n[aã]o encontrada|page not found|oportunidade (encerrada|indispon[ií]vel)", re.I)


@dataclass
class FormField:
    q: FieldQ
    selector: str
    tag: str
    input_type: str
    option_ids: dict[str, str] = field(default_factory=dict)
    prefix: str = ""          # tel: código do país já presente no campo (ex.: "+55")

    @property
    def is_filled(self) -> bool:
        return bool(self.q.current)


@dataclass
class FlowResult:
    status: str                 # applied | failed | needs_user | dry_run | already_applied | limit
    reason: str = ""
    steps: int = 0
    answers: list[dict] = field(default_factory=list)


# ---------- scan ----------
NUM_LABEL_RE = re.compile(r"^\d{3,}$")


def _payload_question_text(html: str, qid: str) -> str:
    """Enunciado de uma pergunta cujo rótulo veio só como id numérico.

    A GeekHunter rotula o <input> com o id da pergunta ("144315") e deixa o texto
    ("Quantos anos de experiência com Java 17 você tem?") no payload da página. Sem o texto
    nenhuma regra responde, e o campo é obrigatório e eliminatório: a etapa trava.
    """
    m = re.search(r'\\?"id\\?"\s*:\s*' + re.escape(qid) + r'\s*,.{0,400}?\\?"name\\?"\s*:\s*\\?"(.*?)\\?"[,}]',
                  html, re.S)
    if not m:
        return ""
    txt = m.group(1)
    try:
        txt = txt.encode("utf-8").decode("unicode_escape").encode("latin-1", "ignore").decode("utf-8", "ignore")
    except Exception:
        pass
    return re.sub(r"\s+", " ", txt).strip()[:200]


def _fix_duplicated_labels(raw: list[dict]) -> int:
    """Vários campos com o MESMO rótulo (na DBC, o título da página): usa o placeholder de cada um.

    Sem isso todo campo recebia "Preencha o formulário abaixo para concluir sua candidatura":
    as regras respondiam a esmo, o currículo não subia e a data nunca era preenchida.
    """
    from collections import Counter
    cont = Counter((r.get("label") or "").strip() for r in raw if (r.get("label") or "").strip())
    trocados = 0
    for r in raw:
        lab = (r.get("label") or "").strip()
        ph = (r.get("placeholder") or "").strip()
        if lab and ph and ph != lab and cont[lab] >= 3:
            r["label"] = ph
            trocados += 1
    return trocados


def scan(scope: Scope, root_selector: str | None = None) -> list[FormField]:
    try:
        raw = scope.evaluate(SCAN_JS, root_selector)
    except Exception:
        return []
    _fix_duplicated_labels(raw)
    out: list[FormField] = []
    for r in raw:
        q = FieldQ(
            id=r["id"], label=r.get("label") or "", kind=r.get("kind") or "text",
            options=[o for o in (r.get("options") or []) if o], required=bool(r.get("required")), current=r.get("value") or "",
        )
        ff = FormField(q=q, selector=f'[data-vagas-id="{r["id"]}"]', tag=r["tag"], input_type=r["type"], option_ids=r.get("optionIds") or {})
        ff.prefix = r.get("prefix") or ""
        out.append(ff)
    # rótulo que é só um id numérico: busca o enunciado no payload da página (GeekHunter)
    sem_texto = [ff for ff in out if NUM_LABEL_RE.fullmatch((ff.q.label or "").strip())]
    if sem_texto:
        try:
            html = scope.content()
        except Exception:
            html = ""
        for ff in sem_texto:
            txt = _payload_question_text(html, ff.q.label.strip()) if html else ""
            if txt:
                ff.q.label = txt
    return out


def _modal_frames(page: Page) -> tuple[list, bool]:
    """(iframes grandes já carregados, existe iframe grande ainda carregando?).
    Grandes = cobrem >= 25% da tela (questionários embutidos: InHire form-app, Typeform...)."""
    ready, loading = [], False
    try:
        vp = page.evaluate("() => ({w: innerWidth, h: innerHeight})")
    except Exception:
        vp = {"w": 1366, "h": 800}
    try:
        iframes = page.locator("iframe")
        n = min(iframes.count(), 20)
    except Exception:
        return [], False
    for i in range(n):
        el = iframes.nth(i)
        try:
            box = el.bounding_box()
            src = el.get_attribute("src") or ""
        except Exception:
            continue
        if not box or not src.startswith("http"):
            continue
        if re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|intercom|hotjar|analytics|gtm|doubleclick", src):
            continue
        big = box["width"] * box["height"] >= 0.25 * vp["w"] * vp["h"]
        # iframe-resizer (InHire form-app): largura cheia e altura 0 até o documento carregar
        wide = box["width"] >= 0.4 * vp["w"]
        if not (big or wide):
            continue
        try:
            handle = el.element_handle(timeout=1000)
            fr = handle.content_frame() if handle else None
        except Exception:
            fr = None
        if fr is None or not fr.url or fr.url == "about:blank":
            loading = True
            continue
        if big or _frame_has_controls(fr):
            ready.append(fr)
        else:
            loading = True
    return ready, loading


def _frame_has_controls(fr) -> bool:
    try:
        return bool(fr.evaluate("() => document.querySelectorAll('input, textarea, select, button, [role=button]').length"))
    except Exception:
        return False


def best_scope(page: Page, root_selector: str | None = None, wait_modal_s: float = 8.0, wait_loading_s: float = 60.0) -> tuple[Scope, list[FormField]]:
    """Escaneia a página e os iframes. Um iframe-modal grande (questionário embutido) tem prioridade;
    senão devolve o escopo com mais campos. Um iframe-modal presente mas ainda sem documento (about:blank,
    InHire form-app carrega devagar) ganha até `wait_loading_s`."""
    if root_selector:
        return (page, scan(page, root_selector))
    t0 = time.time()
    while True:
        modal, loading = _modal_frames(page)
        ready = [fr for fr in modal if _frame_has_controls(fr)]
        if ready:
            fr = ready[0]
            try:
                return (fr, scan(fr, None))
            except Exception:
                pass
        elapsed = time.time() - t0
        if (loading and elapsed < wait_loading_s) or (modal and elapsed < wait_modal_s):
            time.sleep(0.7)   # iframe-modal carregando
            continue
        break
    best: tuple[Scope, list[FormField]] = (page, scan(page, None))
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        try:
            fields = scan(fr, None)
        except Exception:
            continue
        if len(fields) > len(best[1]):
            best = (fr, fields)
    return best



def _close_list(loc: Locator) -> None:
    """Fecha uma lista aberta. Dentro de um diálogo (Easy Apply do LinkedIn) Escape fecha o MODAL e abre
    "Salvar esta candidatura?": nesse caso só tira o foco."""
    try:
        in_dialog = bool(loc.evaluate("el => !!el.closest('[role=\"dialog\"], .artdeco-modal, [aria-modal=\"true\"]')"))
    except Exception:
        in_dialog = False
    try:
        if in_dialog:
            loc.evaluate("el => el.blur()")
        else:
            loc.press("Escape")
    except Exception:
        pass


def prefetch_combobox_options(scope: Scope, fields: list[FormField]) -> None:
    """Abre cada combobox sem opções conhecidas para ler a lista (fecha com Escape)."""
    for f in fields:
        if f.q.options or f.q.current:
            continue
        if f.q.kind != "combobox" and not (f.q.kind == "multiselect" and not f.option_ids):
            continue
        try:
            loc = scope.locator(f.selector)
            loc.click(timeout=3000)
            time.sleep(0.7)
            opts = _visible_options(scope)
            # lista que ainda está carregando (react-select/Greenhouse): espera estabilizar até ~3 s
            deadline = time.time() + 3.0
            while time.time() < deadline:
                time.sleep(0.6)
                again = _visible_options(scope)
                if len(again) == len(opts):
                    break
                opts = again
            if opts:
                f.q.options = opts[:3000]   # listas completas (cidades, áreas): regras precisam ver tudo; o LLM recebe só um recorte
            _close_list(loc)
            time.sleep(0.2)
        except Exception:
            pass


OPTION_SEL = ('[role="option"], li[role="option"], .MuiAutocomplete-option, [class*="option" i][id*="option" i], [class*="select__option" i], '
              'ul[role="listbox"] li, [role="listbox"] button, ul[class*="list" i] li, [class*="suggest" i] li, [class*="autocomplete" i] li, '
              '[class*="dropdown" i] li, [class*="menu" i] [role="menuitem"], [class*="options" i] button, [class*="result" i] li, '
              '.react-dropdown-select-item, [class*="select-item" i], [class*="dropdown-item" i], [class*="menu-item" i], [class*="dropdown-select-dropdown" i] span, '
              'ul[role="menu"] li, [role="menuitemcheckbox"], [role="menuitemradio"], [role="dialog"] ul li')


OPTIONS_JS = r"""
(sel) => {
""" + DEEP_JS_HELPERS + r"""
  const isVisible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const all = deepQuery(document, sel).filter(isVisible);
  const set = new Set(all);
  const out = [];
  let i = 0;
  for (const el of all) {
    // só o elemento mais externo: descarta spans/divs aninhados dentro de outra opção
    let p = el.parentElement, nested = false;
    while (p) { if (set.has(p)) { nested = true; break; } p = p.parentElement; }
    if (nested) continue;
    const raw = (el.innerText || el.textContent || '').trim();
    if (!raw || raw.length > 400) continue;
    // "Título\nDescrição" (react-dropdown-select, MUI) → o rótulo é a primeira linha
    const lines = raw.split(/\n+/).map(t => t.trim()).filter(Boolean);
    const label = lines[0] || raw;
    const id = 'opt' + (i++);
    el.setAttribute('data-vagas-opt', id);
    out.push({ id, label, full: lines.join(' | ') });
    if (out.length >= 3000) break;
  }
  return out;
}
"""


def _visible_options_detailed(scope: Scope) -> list[dict]:
    """Opções visíveis (listas/dropdowns abertos): [{id, label, full}] com marcação data-vagas-opt para clique."""
    try:
        return scope.evaluate(OPTIONS_JS, OPTION_SEL) or []
    except Exception:
        return []


OPTION_JUNK_RE = re.compile(r"obrigat[oó]ri|required|inv[aá]lid|campo (deve|precisa)|preencha|selecione (uma|um|a) op|carregando|loading|nenhum resultado|no results|sem resultados", re.I)


def _visible_options(scope: Scope) -> list[str]:
    out: list[str] = []
    for o in _visible_options_detailed(scope):
        t = o.get("label") or ""
        if t and t not in out and not OPTION_JUNK_RE.search(t):   # mensagens de validação/placeholder não são opções
            out.append(t)
    return out


def close_open_dropdowns(scope: Scope) -> bool:
    """Fecha lista suspensa esquecida aberta, que cobre a página.

    Na Recrutaí o seletor de país do telefone ficava aberto: escondia o botão de enviar e ainda
    enchia a lista de "botões visíveis" com nomes de países ("Afghanistan", "BRASIL (+55)").
    Segue a regra de `_close_list`: dentro de diálogo nunca usa Escape, só tira o foco.
    """
    try:
        antes = len(_visible_options(scope))
    except Exception:
        return False
    if antes < 5:
        return False
    try:
        em_dialogo = bool(scope.evaluate("() => !!document.querySelector('[role=\"dialog\"], .artdeco-modal, [aria-modal=\"true\"]')"))
    except Exception:
        em_dialogo = False
    try:
        if em_dialogo:
            scope.evaluate("() => { const a = document.activeElement; if (a && a.blur) a.blur(); }")
        else:
            scope.locator("body").press("Escape")
        time.sleep(0.4)
    except Exception:
        return False
    try:
        return len(_visible_options(scope)) < antes
    except Exception:
        return False


# ---------- fill ----------
SELECTED_JS = """el => el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-pressed') === 'true' || /(^|\\s|__)(selected|checked|is-selected|is-checked|active)(\\s|$|--)/i.test(el.className || '')"""


def _click_option(scope: Scope, field: FormField, label: str) -> bool:
    oid = field.option_ids.get(label)
    if not oid and field.option_ids:
        m = best_option(label, list(field.option_ids.keys()))
        if m:
            oid = field.option_ids[m]
    if not oid:
        return False
    inp = scope.locator(f'[data-vagas-id="{oid}"]')
    # opção customizada (div/span sem <input>, ex.: Quill Forms/InHire): clica e confere a marcação de selecionado
    try:
        tag = (inp.evaluate("el => el.tagName") or "").lower()
    except Exception:
        tag = "input"
    if tag not in ("input", "option"):
        for attempt in range(2):
            try:
                if inp.evaluate(SELECTED_JS):
                    return True
                h.click(inp, timeout=4000)
                time.sleep(0.5)
                if inp.evaluate(SELECTED_JS):
                    return True
            except Exception:
                pass
        try:
            return bool(inp.evaluate(SELECTED_JS))
        except Exception:
            return False

    def checked() -> bool | None:
        try:
            return inp.is_checked()
        except Exception:
            return None
    if checked():
        return True
    # 1) check() direto (funciona quando o input é clicável)
    try:
        inp.check(timeout=2500)
        if checked():
            return True
    except Exception:
        pass
    # 2) rótulo: clica na borda esquerda (o centro pode ser um link, ex.: "Política de Privacidade").
    #    Sem id (Chakra/MUI: <label><input hidden><span class="control"/>texto</label>) usa o <label> ancestral
    try:
        el_id = inp.get_attribute("id")
    except Exception:
        el_id = None
    lab = None
    if el_id:
        lab = scope.locator(f'label[for="{el_id}"]').first
    if lab is None or not lab.count():
        try:
            anc = inp.locator("xpath=ancestor::label[1]")
            lab = anc if anc.count() else None
        except Exception:
            lab = None
    if lab is not None:
        try:
            if lab.count():
                box = lab.bounding_box()
                if box:
                    lab.click(position={"x": min(10, box["width"] / 2), "y": box["height"] / 2}, timeout=3000)
                else:
                    lab.click(timeout=3000)
                h.tiny()
                if checked() is not False:
                    return True
        except Exception:
            pass
    # 3) clique forçado no próprio input
    try:
        inp.click(force=True, timeout=3000)
        h.tiny()
        return checked() is not False
    except Exception:
        return False



def _same_typed(got: str, want: str, kind: str) -> bool:
    g, w = (got or "").strip(), (want or "").strip()
    if g == w:
        return True
    # máscaras (telefone/CPF/moeda) reformatam: compara só dígitos quando o valor é numérico
    if kind in ("tel", "number") or re.fullmatch(r"[\d\s().+-]+", w):
        return re.sub(r"\D", "", g) == re.sub(r"\D", "", w)
    return g.lower() == w.lower()


def _verify_typed(loc: Locator, want: str, kind: str) -> None:
    """Inputs controlados (React) às vezes embaralham a digitação humanizada (caret pula a cada re-render:
    'fulano@' virou 'fuulaon@'). Confere o valor e, se divergir, usa fill() e por fim JS+eventos."""
    try:
        got = loc.input_value(timeout=1500)
    except Exception:
        return
    if _same_typed(got, want, kind):
        return
    try:
        loc.fill(want)
        h.tiny()
        if _same_typed(loc.input_value(timeout=1500), want, kind):
            return
    except Exception:
        pass
    try:
        loc.evaluate(
            """(el, v) => { const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                 const setter = Object.getOwnPropertyDescriptor(proto, 'value').set; setter.call(el, v);
                 el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""",
            want,
        )
    except Exception:
        pass

DATE_MASK_RE = re.compile(r"(D{2}|DD)\s*/\s*(M{2}|MM)\s*/\s*(Y{4}|A{4})|(M{2})\s*/\s*(D{2})\s*/\s*(Y{4})", re.I)


def _number_value(label: str, value) -> str:
    """Valor para <input type=number>, entendendo o formato brasileiro.

    Documento com pontuação ("123.456.789-00") era enviado com os pontos e o campo recusava:
    CPF vazio travava a etapa (Blite Tecnologia, 11/09). E "R$ 11.000" virava 11 (bug antigo).
    """
    bruto = str(value)
    if re.search(r"\bcpf\b|\bcnpj\b|\brg\b|documento|identidade|\bcep\b|telefone|celular|whatsapp|matr[ií]cula", label, re.I):
        return re.sub(r"\D", "", bruto)                       # identificador: só dígitos
    v = re.sub(r"[^\d.,]", "", bruto)
    if not v:
        return ""
    if "," in v and "." in v:
        return v.replace(".", "").replace(",", ".")           # "9.000,00" -> 9000.00
    if "," in v:
        return v.replace(",", ".")                            # "68,75" -> 68.75
    partes = v.split(".")
    if len(partes) > 2 or (len(partes) == 2 and len(partes[1]) == 3):
        return "".join(partes)                                # "11.000" -> 11000 · "123.456.789" -> dígitos
    return v


def _masked_date(loc, value) -> str | None:
    """Data ISO (1990-05-12) num campo com máscara -> "12/05/1990".

    O formato pode vir no placeholder ("DD/MM/AAAA"), no atributo `mask` do Angular
    ("00/00/0000", DBC Company) ou só no rótulo ("Data de Nascimento*"). Sem isso a data ia
    em ISO e o campo recusava com "Data inválida".
    """
    s = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        partes = [loc.get_attribute(a) or "" for a in ("placeholder", "aria-label", "mask", "data-mask", "inputmask", "title")]
    except Exception:
        return None
    txt = " ".join(partes)
    up = txt.upper()
    mascara_digitos = re.search(r"0{2}\s*/\s*0{2}\s*/\s*0{4}|9{2}\s*/\s*9{2}\s*/\s*9{4}|#{2}\s*/\s*#{2}\s*/\s*#{4}", txt)
    data_no_rotulo = re.search(r"nascimento|birth ?date|date of birth|\bdata\b", txt, re.I)
    if not (DATE_MASK_RE.search(up) or mascara_digitos or data_no_rotulo):
        return None
    y, m, d = s.split("-")
    return f"{m}/{d}/{y}" if re.search(r"M{2}\s*/\s*D{2}", up) else f"{d}/{m}/{y}"


_RESUME_MEMO: tuple[object, Path | None] | None = None


def _resolve_resume(resume_path: Path | str | Callable[[], Path | None] | None) -> Path | None:
    """Devolve um PDF de currículo que existe no disco, ou None.

    Aceita o caminho pronto ou a função PREGUIÇOSA de `ApplyContext.resume` — o PDF só é
    gerado quando o formulário realmente pede o arquivo. O resultado da função é memorizado
    porque o loop de etapas resolve o currículo a cada passo, e gerar de novo custaria uma
    chamada de IA e vários segundos. A memória guarda o próprio objeto da função (comparado
    por identidade): cada candidatura tem a sua, então nunca reaproveita currículo de outra.

    `ResumeBuildError` SOBE de propósito: `fill_field` e o pipeline tratam falha de geração
    como vaga para recolocar na fila, não como campo que não deu para preencher.
    """
    global _RESUME_MEMO
    alvo = resume_path
    if callable(alvo):
        if _RESUME_MEMO is not None and _RESUME_MEMO[0] is alvo:
            alvo = _RESUME_MEMO[1]
        else:
            gerado = alvo()
            _RESUME_MEMO = (alvo, gerado)   # referência forte: impede reuso de identidade
            alvo = gerado
    if not alvo:
        return None
    rp = Path(alvo)
    try:
        if not rp.is_file() or rp.stat().st_size == 0:
            return None
    except OSError:
        return None
    return rp


def fill_field(scope: Scope, f: FormField, value: Any,
               resume_path: Path | str | Callable[[], Path | None] | None) -> bool:
    kind = f.q.kind
    loc = scope.locator(f.selector)
    if kind in ("combobox", "text", "date"):   # datepicker com máscara: digitar no formato do site
        masked = _masked_date(loc, value)
        if masked:
            for attempt in (re.sub(r"\D", "", masked), masked):   # máscara do site já põe as barras
                try:
                    loc.click()
                    h.tiny()
                    try:
                        loc.fill("")
                    except Exception:
                        pass
                    loc.type(attempt, delay=70)
                    h.tiny()
                    got = re.sub(r"\D", "", loc.input_value() or "")
                    if got == re.sub(r"\D", "", masked):
                        _close_list(loc)   # fecha o datepicker sem Escape dentro de modal
                        return True
                except Exception:
                    continue
    try:
        if kind == "file":
            rp = _resolve_resume(resume_path)
            if not rp:
                return False
            loc.set_input_files(str(rp))
            h.short()
            return True
        if value is None or value == "":
            return False
        if kind == "select":
            try:
                loc.select_option(label=str(value))
            except Exception:
                m = process.extractOne(str(value), f.q.options, scorer=fuzz.token_set_ratio)
                if not m or m[1] < 55:
                    return False
                loc.select_option(label=m[0])
            h.tiny()
            return True
        if kind == "radio":
            return _click_option(scope, f, str(value))
        if kind == "checkbox":
            truthy = str(value).strip().lower() in ("true", "sim", "yes", "1", "aceito", "concordo", "on")
            try:
                checked = loc.is_checked()
            except Exception:
                try:   # botão-checkbox (Radix/shadcn): estado em aria-checked/data-state
                    checked = bool(loc.evaluate("el => el.getAttribute('aria-checked') === 'true' || el.getAttribute('data-state') === 'checked'"))
                except Exception:
                    checked = False
            if truthy and not checked:
                return _click_option(scope, FormField(f.q, f.selector, f.tag, f.input_type, {"x": f.q.id}), "x")
            return True
        if kind == "multiselect":
            vals = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
            if not f.option_ids:
                return _fill_combobox_multi(scope, loc, [str(v) for v in vals], f)
            ok = False
            for v in vals:
                ok = _click_option(scope, f, v) or ok
            return ok
        if kind == "combobox":
            return _fill_combobox(scope, loc, str(value), f)
        if kind == "date":
            v = str(value)
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", v):  # dd/mm/aaaa -> ISO
                d, m, y = v.split("/")
                v = f"{y}-{m}-{d}"
            try:
                loc.fill(v)
            except Exception:
                h.type_text(loc, str(value))
            return True
        if kind == "number":
            loc.fill(_number_value(f.q.label or "", value) or "0")
            h.tiny()
            return True
        # text / textarea / email / tel / url
        s = str(value)
        if re.search(r"somente n[uú]meros|apenas n[uú]meros|s[oó] n[uú]meros|only numbers|digits only|sem pontua", f.q.label or "", re.I):
            s = re.sub(r"\D", "", s)   # "CPF (Somente números)", "CEP (apenas números)"
        if kind == "tel":
            # seletor de país ao lado (intl-tel-input / vue-tel-input: Recrutei, InHire) -> só o número nacional
            try:
                has_country = bool(loc.evaluate(
                    "el => { const w = el.closest('[class*=\"phone\" i], [class*=\"tel\" i], .iti, .vue-tel-input, [class*=\"intl\" i]') || el.parentElement;"
                    " return !!(w && w.querySelector('[name*=\"country\" i], [class*=\"country\" i], .iti__selected-flag, [class*=\"flag\" i], select')); }"))
            except Exception:
                has_country = False
            pt_page = False
            if not has_country:
                try:   # site em pt-BR sem seletor de país: quer o número nacional, não "+55 ..."
                    pt_page = bool(loc.evaluate("el => /^pt/i.test(el.ownerDocument.documentElement.lang || '')"))
                except Exception:
                    pt_page = False
            if f.prefix or has_country or pt_page or re.search(r"\+55|brasil", (f.q.label or "").lower()):
                digits = re.sub(r"\D", "", s)
                if digits.startswith("55") and len(digits) > 11:
                    digits = digits[2:]
                s = digits
        try:
            try:
                ml = loc.get_attribute("maxlength")
                if ml and ml.isdigit() and int(ml) > 0 and len(s) > int(ml):
                    s = s[: int(ml)].rstrip()
            except Exception:
                pass
            if len(s) > 250 or kind == "textarea" and len(s) > 120:
                loc.fill(s)
                h.tiny()
            else:
                before = set(_visible_buttons_text(scope))
                if kind == "text":
                    # gatilho de dropdown disfarçado de texto (ex.: "Informe sua cidade" na InHire): só conta se a
                    # lista/campo de busca APARECEU depois do clique (páginas cheias de <li> não são dropdown)
                    opts_before = set(_visible_options(scope))
                    search_before = _dropdown_search_input(scope, exclude=loc) is not None
                    try:
                        loc.click(timeout=3000)
                        time.sleep(0.5)
                    except Exception:
                        pass
                    new_opts = [o for o in _visible_options(scope) if o not in opts_before]
                    if (not search_before and _dropdown_search_input(scope, exclude=loc) is not None) or len(new_opts) >= 5:
                        _close_list(loc)
                        return _fill_combobox(scope, loc, s, f)
                h.type_text(loc, s)
                _verify_typed(loc, s, kind)
                if kind == "text":
                    _pick_suggestion_if_any(scope, s, before)
            return True
        except Exception:
            digits = re.sub(r"\D", "", s)
            if digits and kind in ("text", "tel"):
                loc.fill(digits)    # campos com máscara (moeda/telefone) aceitam só dígitos
                h.tiny()
                return True
            raise
    except ResumeBuildError:
        raise                      # falha ao gerar currículo sobe: o pipeline recoloca a vaga na fila
    except Exception:
        return False


def _visible_buttons_text(scope: Scope) -> list[str]:
    try:
        return scope.evaluate("""() => Array.from(document.querySelectorAll('button, [role=option], li')).filter(e => e.offsetWidth && e.offsetHeight).map(e => (e.innerText||'').trim()).filter(t => t && t.length < 80)""")
    except Exception:
        return []


def _pick_suggestion_if_any(scope: Scope, value: str, before: set[str]) -> bool:
    """Autocomplete disfarçado de texto (cidade, país, cargo): se apareceu uma lista após digitar, escolhe a melhor opção.
    Listas enormes (todas as cidades do Brasil) são aceitas: o fuzzy acha a certa."""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        time.sleep(0.6)
        opts = _visible_options(scope)
        if not opts:
            after = _visible_buttons_text(scope)
            opts = [t for t in after if t not in before]
        if not opts:
            continue
        if _click_matching_option(scope, value, opts, min_score=70):
            return True
    return False


def _click_matching_option(scope: Scope, value: str, opts: list[str], min_score: int = 72) -> bool:
    if re.fullmatch(r"\s*\d[\d.,]*\s*", value or ""):
        # valor numérico (salário, anos): só opção que contenha o número ou faixa que o inclua; nunca fuzzy
        from .qa import salary_range_option
        digits = re.sub(r"\D", "", value)
        target = next((o for o in opts if digits and digits in re.sub(r"\D", "", o)), None)
        if not target and digits:
            try:
                target = salary_range_option(int(digits), opts)
            except Exception:
                target = None
    else:
        target = best_option(value, opts[:8000], min_score=min_score)
    if not target:
        return False
    # 1) elemento marcado pelo scan de opções (texto exato da primeira linha)
    for o in _visible_options_detailed(scope):
        if o.get("label") == target:
            try:
                cand = scope.locator(f'[data-vagas-opt="{o["id"]}"]').first
                if cand.count() and cand.is_visible():
                    h.click(cand, timeout=3000)
                    h.tiny()
                    return True
            except Exception:
                break
    # 2) reserva: por texto
    first_line = target.split("\n")[0].strip()
    for sel in (OPTION_SEL, "button", "li", "[role=option]", "[class*='item' i]", "span", "div"):
        try:
            cand = scope.locator(sel).filter(has_text=first_line).first
            if cand.count() and cand.is_visible():
                cand.click(timeout=3000)
                h.tiny()
                return True
        except Exception:
            continue
    return False


def _dropdown_search_input(scope: Scope, exclude: Locator | None = None):
    """Campo de busca dentro de um dropdown aberto (react-dropdown-select, MUI, etc.)."""
    excl_id = None
    if exclude is not None:
        try:
            excl_id = exclude.get_attribute("data-vagas-id")
        except Exception:
            excl_id = None
    try:
        loc = scope.locator("input[placeholder]")
        for i in range(min(loc.count(), 40)):
            el = loc.nth(i)
            try:
                ph = (el.get_attribute("placeholder") or "").lower()
                if not re.search(r"pesquis|busca|search|filtr|digite|procur", ph):
                    continue
                if not el.is_visible():
                    continue
                if excl_id and el.get_attribute("data-vagas-id") == excl_id:
                    continue
                return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def _typing_target(loc: Locator) -> Locator:
    """Onde digitar: o próprio elemento se for input/textarea/contenteditable; senão o <input> interno (div role=combobox)."""
    try:
        if loc.evaluate("el => el.matches('input, textarea, [contenteditable]')"):
            return loc
        inner = loc.locator("input:not([type='hidden'])").first
        if inner.count() and inner.is_visible():
            return inner
    except Exception:
        pass
    return loc


def _type_into(loc: Locator, typer: Locator, text: str, delay: int = 60) -> None:
    """Digita no alvo; se ele não aceita teclado (div sem input visível), digita pelo teclado da página no elemento focado."""
    try:
        typer.press_sequentially(text, delay=delay)
        return
    except Exception:
        pass
    try:
        loc.click(timeout=2000)
    except Exception:
        pass
    loc.page.keyboard.type(text, delay=delay)



def _clear_typed(loc: Locator, typer: Locator) -> None:
    """Limpa o texto digitado num combobox (o campo pode ser um <input> invisível dentro de um div: Ctrl+A falha)."""
    for fn in (lambda: (typer.press("Control+a"), typer.press("Backspace")),
               lambda: typer.fill(""),
               lambda: loc.page.keyboard.press("Control+a") or loc.page.keyboard.press("Backspace")):
        try:
            fn()
            cur = ""
            try:
                cur = typer.input_value(timeout=800) or ""
            except Exception:
                cur = ""
            if not cur.strip():
                return
        except Exception:
            continue
    try:   # último recurso: apagar caractere a caractere pelo teclado da página
        for _ in range(60):
            loc.page.keyboard.press("Backspace")
    except Exception:
        pass


def _fill_combobox(scope: Scope, loc: Locator, value: str, f: FormField) -> bool:
    """Abre a lista e clica na opção certa. Se o dropdown tiver campo de busca próprio, digita nele.
    Só digita no próprio campo se a lista não aparecer (autocomplete). Nunca escolhe às cegas."""
    typer = _typing_target(loc)
    try:
        before = set(_visible_buttons_text(scope))
        loc.click(timeout=4000)
        time.sleep(0.6)
        search = _dropdown_search_input(scope, exclude=loc)
        if search is not None:
            try:
                search.click(timeout=2000)
                search.fill(value[:60])
            except Exception:
                try:
                    search.press_sequentially(value[:60], delay=50)
                except Exception:
                    pass
            deadline = time.time() + 3.5
            while time.time() < deadline:
                time.sleep(0.6)
                opts = _visible_options(scope) or [t for t in _visible_buttons_text(scope) if t not in before]
                if opts and _click_matching_option(scope, value, opts):
                    return True
        opts = _visible_options(scope) or [t for t in _visible_buttons_text(scope) if t not in before]
        if opts and _click_matching_option(scope, value, opts):
            return True
        # autocomplete: digita e espera a lista filtrar (no input interno quando o campo é um div role=combobox)
        try:
            typer.press("Control+a"); typer.press("Backspace")
        except Exception:
            pass
        # tenta com o valor inteiro e, se a lista não filtrar, com prefixos menores ("Tecnologia da Informação" -> "Tecnologia")
        words = [w for w in re.split(r"\s+", value.strip()) if w]
        probes = [value[:60]]
        if len(words) > 2:
            probes.append(" ".join(words[:2]))
        if len(words) > 1:
            probes.append(words[0])
        for probe in probes:
            _clear_typed(loc, typer)
            try:
                _type_into(loc, typer, probe, delay=60)
            except Exception:
                return False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                time.sleep(0.6)
                opts = _visible_options(scope) or [t for t in _visible_buttons_text(scope) if t not in before]
                if opts and _click_matching_option(scope, value, opts):
                    return True
            if probe == probes[0]:
                # combos que só confirmam com Enter (Indeed): digitou o valor exato e a lista não apareceu
                try:
                    loc.page.keyboard.press("Enter")
                    time.sleep(0.6)
                    cur = (typer.input_value(timeout=800) or "").strip()
                    if cur and _norm_opt(cur).startswith(_norm_opt(value)[:6]):
                        return True
                except Exception:
                    pass
        # não achou: apaga o que digitou (senão o campo fica com lixo); se a lista tem "Outra/Other/Não listada", usa
        try:
            typer.press("Control+a"); typer.press("Backspace")
            time.sleep(0.5)
        except Exception:
            pass
        try:
            OTHER_RE = re.compile(r"\s*(outr[ao]s?|other|others|n[aã]o listad[ao]|not listed|nenhuma das (anteriores|opções)|não encontrei.*)\s*", re.I)
            opts_all = _visible_options(scope)
            other = next((o for o in opts_all if OTHER_RE.fullmatch(o)), None)
            if not other:
                # lista longa/assíncrona (escolas no Greenhouse): procurar digitando
                for probe in ("Other", "Outra", "Outro"):
                    try:
                        _type_into(loc, typer, probe, delay=40)
                        time.sleep(0.8)
                        opts_all = _visible_options(scope)
                        other = next((o for o in opts_all if OTHER_RE.fullmatch(o)), None)
                        typer.press("Control+a"); typer.press("Backspace")
                        if other:
                            _type_into(loc, typer, probe, delay=40); time.sleep(0.6)
                            opts_all = _visible_options(scope)
                            break
                    except Exception:
                        continue
            if other and _click_matching_option(scope, other, opts_all, min_score=95):
                return True
        except Exception:
            pass
        _close_list(loc)
        return False
    except Exception:
        return False



def _fill_combobox_multi(scope: Scope, loc: Locator, values: list[str], f: FormField) -> bool:
    """Combobox de seleção múltipla (Indeed 'Selecione os requisitos...'): abre, e para cada valor filtra no campo de
    busca (se houver) e clica na opção; a lista fica aberta entre marcações; fecha no fim sem Escape em diálogos."""
    ok_any = False
    try:
        # se o popup já está aberto (sobrou do prefetch), não clica de novo (o popup cobre o campo e o clique é interceptado)
        if _dropdown_search_input(scope, exclude=loc) is None and not _visible_options(scope):
            loc.click(timeout=4000)
        time.sleep(0.6)
    except Exception:
        try:
            loc.click(force=True, timeout=3000)
            time.sleep(0.6)
        except Exception:
            return False
    for v in values:
        try:
            search = _dropdown_search_input(scope, exclude=loc)
            if search is not None:
                search.fill("")
                search.fill(v[:60])
                time.sleep(0.5)
            opts = _visible_options(scope)
            if opts and _click_matching_option(scope, v, opts, min_score=75):
                ok_any = True
                time.sleep(0.3)
        except Exception:
            continue
    try:
        search = _dropdown_search_input(scope, exclude=loc)
        if search is not None:
            search.fill("")
    except Exception:
        pass
    _close_list(loc)
    try:
        if _visible_options(scope):   # popup ainda aberto (Indeed ignora Escape): fecha clicando fora
            loc.page.mouse.click(5, 5)
            time.sleep(0.3)
    except Exception:
        pass
    return ok_any


UPLOAD_BTN_RE = re.compile(r"^(upload|fazer upload|anexar|adicionar( arquivo| curr[ií]culo| cv)?|carregar( arquivo| curr[ií]culo)?|escolher arquivo|selecionar arquivo|choose file|browse|attach( resume| cv| file)?|add (a )?file|enviar arquivo)\b", re.I)


def upload_via_chooser(page: Page, resume_path: Path | str | Callable[[], Path | None] | None) -> bool:
    """Formulários sem <input type=file> no DOM (Notion Forms, alguns Typeform): botão "Upload" abre o seletor do sistema.
    Clica no botão de upload mais próximo de um rótulo de currículo com expect_file_chooser e envia o PDF."""
    if resume_path is None:
        return False
    try:
        cands = page.locator("button, [role='button'], label, a")
        n = min(cands.count(), 80)
    except Exception:
        return False
    best = None
    for i in range(n):
        el = cands.nth(i)
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text(timeout=300) or "").strip()
            if not t or not UPLOAD_BTN_RE.search(t):
                continue
            near = el.evaluate("e => { let p = e; for (let k = 0; k < 4 && p; k++, p = p.parentElement) { const t = (p.innerText || ''); if (/curr[ií]cul|resume|\\bcv\\b/i.test(t)) return true; } return false; }")
            if near:
                best = el
                break
            if best is None:
                best = el
        except Exception:
            continue
    if best is None:
        return False
    rp = _resolve_resume(resume_path)   # só aqui vale gerar o PDF
    if not rp:
        return False
    try:
        with page.expect_file_chooser(timeout=5000) as fc:
            best.click(timeout=4000)
        fc.value.set_files(str(rp))
        time.sleep(1.5)
        return True
    except Exception:
        return False

# ---------- botões ----------
def find_action_button(scope: Scope, root_selector: str | None = None, include_disabled: bool = False) -> tuple[Locator, str] | None:
    """Botão de avançar/enviar habilitado. Com include_disabled=True devolve (loc, 'disabled:<kind>') se só houver desabilitado."""
    try:
        btns_all = scope.evaluate(BUTTONS_JS, root_selector)
    except Exception:
        return None
    btns = [b for b in btns_all if not b.get("disabled")]
    if not btns and include_disabled:
        res = _classify_buttons([b for b in btns_all if b.get("disabled")], scope)
        return (res[0], "disabled:" + res[1]) if res else None
    return _classify_buttons(btns, scope)


def _classify_buttons(btns: list[dict], scope: Scope) -> tuple[Locator, str] | None:
    submit, nxt = [], []
    for b in btns:
        label = (b.get("text") or "").strip()
        aria = (b.get("aria") or "").strip()
        for cand in (label, aria):
            if not cand or BAD_RE.search(cand):
                continue
            c = cand.strip().rstrip(".").strip()
            if SUBMIT_RE.match(c):
                submit.append(b); break
            if NEXT_RE.match(c):
                # <button type="submit"> com texto de "continuar" envia o formulário (InHire "Continuar inscrição"): trata como envio
                (submit if b.get("type") == "submit" and not re.match(r"^(avan[cç]ar|pr[oó]xim|next|revisar|review|iniciar|come[cç]ar|start|begin)", c, re.I) else nxt).append(b)
                break
        else:
            if b.get("type") == "submit" and label and not BAD_RE.search(label):
                submit.append(b)
    pick = None
    kind = ""
    if nxt:
        pick, kind = sorted(nxt, key=lambda x: (-x["top"], -x["left"]))[0], "next"
    elif submit:
        pick, kind = sorted(submit, key=lambda x: (-x["top"], -x["left"]))[0], "submit"
    if not pick:
        return None
    return scope.locator(f'[data-vagas-btn="{pick["id"]}"]'), kind


def find_button(scope: Scope, pattern: re.Pattern, root_selector: str | None = None) -> Locator | None:
    try:
        btns = [b for b in scope.evaluate(BUTTONS_JS, root_selector) if not b.get("disabled")]
    except Exception:
        return None
    for b in btns:
        for cand in (b.get("text") or "", b.get("aria") or ""):
            if cand and pattern.search(cand):
                return scope.locator(f'[data-vagas-btn="{b["id"]}"]')
    return None


# ---------- estado da página ----------

def embedded_form_url(page: Page) -> str | None:
    """URL do questionário embutido (InHire form-app etc.), se houver um iframe grande dele na página."""
    try:
        for src in page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(e => e.src || '')"):
            if re.search(r"form-app\.inhire\.app/form|typeform\.com/to/|forms\.office\.com|docs\.google\.com/forms", src or ""):
                return src
    except Exception:
        pass
    return None


def page_text(scope: Scope, root_selector: str | None = None, limit: int = 20000) -> str:
    try:
        if root_selector:
            return scope.locator(root_selector).first.inner_text(timeout=3000)[:limit]
        return scope.locator("body").inner_text(timeout=3000)[:limit]
    except Exception:
        return ""


ERRORS_JS = r"""
(rootSel) => {
""" + DEEP_JS_HELPERS + r"""
  const root = (rootSel && deepOne(document, rootSel)) || document.body;
  const sel = '[role="alert"], [role="tooltip"], [class*="tooltip" i], .artdeco-inline-feedback--error, .artdeco-inline-feedback__message, [class*="error" i]:not([class*="errorless" i]), [class*="invalid" i], .Mui-error, .invalid-feedback, .help-block, [class*="feedback" i], [class*="helper-text" i], [class*="helperText" i], [aria-live="assertive"], [aria-live="polite"]';
  const reallyVisible = (el) => {
    const st = getComputedStyle(el); const r = el.getBoundingClientRect();
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return false;
    if (r.width < 4 || r.height < 4) return false;
    if (/sr-only|visually-hidden|a11y-text|screen-reader/i.test(el.className || '')) return false;
    if (st.clip && /rect\(0/.test(st.clip)) return false;
    return true;
  };
  const out = [];
  for (const el of deepQuery(root, sel)) {
    if (!reallyVisible(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length > 2 && t.length < 240 && !out.includes(t)) out.push(t);
    if (out.length >= 25) break;
  }
  return out;
}
"""


HINT_RE = re.compile(r"pressione (enter|shift)|press (enter|shift)|shift\s*\+\s*enter|ctrl\s*\+\s*enter|para (continuar|avan[cç]ar)$|↵|"
                     r"m[aá]x(imo|\.)? ?(de )?\d+ caracteres|\d+ caracteres restantes|characters? (remaining|left)|max(imum)? \d+ characters|"
                     r"n[aã]o poder[aã]o ser editad|cannot be edited|carregado (agora|com sucesso)|uploaded|arquivo (selecionado|anexado|enviado)|upload conclu|"
                     r"alerta de vagas criado|job alert created|receber[aá] sua primeira atualiza", re.I)


def errors(scope: Scope, root_selector: str | None = None) -> list[str]:
    try:
        out = scope.evaluate(ERRORS_JS, root_selector)
    except Exception:
        return []
    return [t for t in out if not SUCCESS_RE.search(t) and not HINT_RE.search(t)]


def invalid_field_ids(scope: Scope) -> set[str]:
    try:
        return set(scope.evaluate("""() => {""" + DEEP_JS_HELPERS + """ return deepQuery(document, '[aria-invalid="true"][data-vagas-id], .Mui-error [data-vagas-id], [class*="error" i] [data-vagas-id]').map(e => e.getAttribute('data-vagas-id')); }"""))
    except Exception:
        return set()


def dump_learn(page: Page, job_id: str, ats: str, fields: list[FormField], note: str, step: int = 0) -> Path:
    d = DATA_DIR / "learn" / (ats or "generic") / re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)
    d.mkdir(parents=True, exist_ok=True)
    base = d / f"step{step}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    base.with_suffix(".json").write_text(json.dumps({
        "url": page.url, "note": note,
        "fields": [{"id": f.q.id, "label": f.q.label, "kind": f.q.kind, "options": f.q.options, "required": f.q.required, "current": ("<senha>" if f.q.kind == "password" and f.q.current else f.q.current)} for f in fields],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def screenshot_evidence(page: Page, job_id: str) -> Path | None:
    d = DATA_DIR / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', job_id)}.png"
    try:
        page.screenshot(path=str(p))
        return p
    except Exception:
        return None
