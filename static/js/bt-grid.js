// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// BT.Grid — composant de matrice de routage RÉUTILISABLE (tous plugins).
// CONVENTION Bobi.Tools (unique, centralisée ici) : DESTINATIONS en COLONNES (haut),
// SOURCES en LIGNES (gauche). Une cellule [ligne=source, colonne=destination] montre l'état
// du croisement ; cliquer route la destination vers la source.
//
// Le composant gère le rendu, les en-têtes collants, le défilement, le repli/épingle par
// groupe (persisté), le clic-droit « sauter à une catégorie » et, en option, une barre
// « préparé → TAKE ». Le MÉTIER reste au plugin, via des callbacks :
//
//   BT.Grid.mount(hostEl, {
//     sources:      [{ key, label, group?, category?, badge? }],           // → lignes
//     destinations: [{ key, label, group?, category?, badge?, active? }],  // → colonnes
//     cell(src, dst) => ({ state:"on"|"partial"|"off", clickable, title }),
//     onRoute(src, dst),            // clic (ou TAKE si features.takeBar)
//     onDisconnect(dst)?,           // affiche ✕ sur les colonnes où dst.active
//     features: { collapse, pin, jump, takeBar, select }?,   // select → sources sélectionnables (clic en-tête)
//     selectionActions: [{ label, cls?, title?, onClick(selectedKeys, selectedSources) }]?,  // barre d'actions sur la sélection
//     onSelectionChange(selectedKeys)?,
//     labels: { corner?, take?, cancel?, prepared?, selected?, selHint?, clear? }?,
//     persistKey?: "bt:<type>:grid",   // localStorage pour repli/épingle
//
//   Sélection (features.select) : clic sur l'en-tête d'une ligne (source) ou d'un groupe source
//   la sélectionne (surbrillance de la ligne) ; clic normal = sélection unique, Maj+clic = multi.
//   ctrl.getSelection() / ctrl.clearSelection() exposés. La sélection est purgée dans setData().
//   }) => { render(), setData(sources, destinations), destroy() }
(function () {
    "use strict";
    const BT = window.BT = window.BT || {};
    const esc = (s) => (BT.esc ? BT.esc(s) : String(s == null ? "" : s));
    function el(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
    function normGroup(it) { let g = it.group; if (g == null) g = { key: "", label: "" }; else if (typeof g === "string") g = { key: g, label: g }; return g; }
    function catOf(it) { if (it.category) return it.category; const m = String(it.label || "").match(/^[A-Za-z]+/); return m ? m[0] : (it.label || ""); }

    BT.Grid = { mount };

    function mount(host, opts) {
        const cfg = Object.assign({ features: {}, labels: {} }, opts);
        const F = Object.assign({ collapse: true, pin: true, jump: true, takeBar: false, select: false }, cfg.features);
        const L = Object.assign({ corner: "dst ▸ / src ▾", take: "TAKE", cancel: "Annuler", prepared: "Préparé :",
            selected: "source(s) sélectionnée(s)", selHint: "Maj+clic pour en (dé)sélectionner", clear: "Vider" }, cfg.labels);
        let sources = cfg.sources || [], destinations = cfg.destinations || [];
        const st = { collapsed: loadSet(cfg.persistKey, "col"), pinned: loadSet(cfg.persistKey, "pin"), prepared: null, jump: null, selected: new Set() };

        const ctrl = { render, setData, destroy, getSelection: () => [...st.selected], clearSelection: clearSel };
        render();
        return ctrl;

        function setData(s, d) {
            sources = s || sources; destinations = d || destinations;
            if (st.selected.size) { const valid = new Set(sources.map((x) => x.key)); [...st.selected].forEach((k) => { if (!valid.has(k)) st.selected.delete(k); }); }
            render();
        }
        function destroy() { closeJump(); host.innerHTML = ""; }

        // ── Groupes (ordre d'origine, épinglés en tête) ──
        function groups(items, axis) {
            const map = new Map();
            items.forEach((it, i) => { const g = normGroup(it); if (!map.has(g.key)) map.set(g.key, { key: g.key, label: g.label, items: [], order: i }); map.get(g.key).items.push(it); });
            return [...map.values()].sort((a, b) => {
                const pa = st.pinned.has(axis + a.key) ? 0 : 1, pb = st.pinned.has(axis + b.key) ? 0 : 1;
                return pa - pb || a.order - b.order;
            });
        }

        // ── Rendu ──
        function render() {
            closeJump();
            // Préserve la position de défilement à travers le re-render (le polling appelle
            // setData→render toutes les N s ; sans ça la grille « remonte en haut »).
            const prevWrap = host.querySelector(".btg-wrap");
            const savedScroll = prevWrap ? { left: prevWrap.scrollLeft, top: prevWrap.scrollTop } : null;
            const wrap = el("div", "btg-wrap");
            const tbl = el("table", "btg-matrix");
            const thead = document.createElement("thead");

            // colonnes = destinations (aplaties, repli → 1 colonne « mcol »)
            const cols = [], colGroups = groups(destinations, "d:");
            const grpRow = document.createElement("tr");
            const corner = el("th", "btg-corner", L.corner); corner.rowSpan = 2; grpRow.appendChild(corner);
            const jumpCols = [];
            colGroups.forEach((g) => {
                const collapsed = st.collapsed.has("d:" + g.key);
                const span = collapsed ? 1 : g.items.length;
                const gh = el("th", "btg-grouphdr"); gh.colSpan = span;
                gh.appendChild(groupCtrls("d:", g, collapsed));
                grpRow.appendChild(gh);
                jumpCols.push({ label: g.label || "—", el: gh });
                if (collapsed) cols.push({ mcol: true, group: g, label: g.label, items: g.items });
                else g.items.forEach((it) => cols.push({ item: it, group: g }));
            });
            thead.appendChild(grpRow);
            const colRow = document.createElement("tr");
            cols.forEach((c) => {
                const th = el("th", "btg-col" + (c.mcol ? " btg-mcol" : ""));
                if (c.mcol) { th.title = c.label; th.appendChild(el("div", "btg-lbl", c.items.length)); }
                else {
                    th.title = c.item.label;
                    const lbl = el("div", "btg-lbl", c.item.label);
                    badges(c.item.badge).forEach((b) => lbl.appendChild(b));
                    th.appendChild(lbl);
                    if (cfg.onDisconnect && c.item.active) th.appendChild(discBtn(c.item));
                }
                colRow.appendChild(th);
            });
            thead.appendChild(colRow); tbl.appendChild(thead);

            // lignes = sources (par groupe, avec séparateur + repli)
            const tbody = document.createElement("tbody");
            const jumpRows = [];
            groups(sources, "s:").forEach((g) => {
                const collapsed = st.collapsed.has("s:" + g.key);
                if (g.key !== "" || colGroups.length) {           // en-tête de groupe (si groupé)
                    const sep = document.createElement("tr");
                    const th = el("th", "btg-grp-row"); th.colSpan = cols.length + 1;
                    th.appendChild(groupCtrls("s:", g, collapsed));
                    if (F.select && g.items.length) {              // clic sur l'en-tête de groupe = (dé)sélectionne toutes ses sources
                        th.classList.add("btg-selectable");
                        th.title = "Clic : sélectionner le groupe · Maj+clic : ajouter";
                        th.addEventListener("click", (e) => { if (e.shiftKey) e.preventDefault(); selToggle(g.items.map((it) => it.key), e.shiftKey); });
                    }
                    sep.appendChild(th); tbody.appendChild(sep);
                    jumpRows.push({ label: g.label || "—", el: sep });
                }
                if (collapsed) return;
                g.items.forEach((src) => {
                    const tr = document.createElement("tr");
                    if (F.select && st.selected.has(src.key)) tr.className = "btg-rowsel";
                    tr.appendChild(rowHead(src));
                    cols.forEach((c) => tr.appendChild(renderCell(src, c)));
                    tbody.appendChild(tr);
                });
            });
            tbl.appendChild(tbody); wrap.appendChild(tbl);

            host.innerHTML = "";
            if (F.takeBar) host.appendChild(takeBar());
            if (F.select) host.appendChild(selBar());
            host.appendChild(wrap);
            // Restaure le défilement dans le même tour JS (aucun repaint intermédiaire → pas de saut visible).
            if (savedScroll) { wrap.scrollLeft = savedScroll.left; wrap.scrollTop = savedScroll.top; }
            const gh = wrap.querySelector(".btg-grouphdr");
            if (gh) wrap.style.setProperty("--btg-grp-h", gh.offsetHeight + "px");
            if (F.jump) attachJump(wrap, jumpCols, jumpRows);
        }

        function rowHead(src) {
            const sel = F.select && st.selected.has(src.key);
            const th = el("th", "btg-row" + (sel ? " btg-selected" : ""));
            const lbl = el("div", "btg-lbl", src.label);
            badges(src.badge).forEach((b) => lbl.appendChild(b));
            th.appendChild(lbl);
            if (F.select) {
                th.classList.add("btg-selectable");
                th.title = "Clic : sélectionner · Maj+clic : plusieurs";
                th.addEventListener("click", (e) => { if (e.shiftKey) e.preventDefault(); selToggle([src.key], e.shiftKey); });
            }
            return th;
        }

        function renderCell(src, c) {
            if (c.mcol) return el("td", "btg-cell btg-parked");
            const dst = c.item;
            const info = cfg.cell ? (cfg.cell(src, dst) || {}) : {};
            const td = el("td", "btg-cell btg-" + (info.state || "off"));
            if (info.title) td.title = info.title;
            const prepared = st.prepared && st.prepared.src.key === src.key && st.prepared.dst.key === dst.key;
            if (prepared) td.classList.add("btg-prepared");
            if (info.clickable !== false) {
                td.classList.add("btg-click");
                td.addEventListener("click", () => onCell(src, dst));
            }
            const mark = info.state === "on" ? "●" : info.state === "partial" ? "◐" : "";
            td.appendChild(el("span", "btg-dot", mark));
            return td;
        }

        function onCell(src, dst) {
            if (F.takeBar) {
                st.prepared = (st.prepared && st.prepared.src.key === src.key && st.prepared.dst.key === dst.key) ? null : { src, dst };
                render();
            } else if (cfg.onRoute) { cfg.onRoute(src, dst); }
        }

        // ── Contrôles de groupe (repli / épingle) ──
        function groupCtrls(axis, g, collapsed) {
            const box = el("span", "btg-mgrp");
            if (F.collapse) {
                const b = el("button", "btg-toggle", collapsed ? "▸" : "▾");
                b.title = collapsed ? "Déplier" : "Replier";
                b.addEventListener("click", (e) => { e.stopPropagation(); toggle(st.collapsed, axis + g.key); });
                box.appendChild(b);
            }
            if (F.pin) {
                const on = st.pinned.has(axis + g.key);
                const p = el("button", "btg-pin" + (on ? " on" : ""), on ? "★" : "☆");
                p.title = "Épingler en tête";
                p.addEventListener("click", (e) => { e.stopPropagation(); toggle(st.pinned, axis + g.key); });
                box.appendChild(p);
            }
            box.appendChild(el("span", "btg-mname", g.label || "—"));
            return box;
        }

        function discBtn(dst) {
            const b = el("button", "btn btn-red btg-disc", "✕");
            b.title = "Déconnecter";
            b.addEventListener("click", (e) => { e.stopPropagation(); cfg.onDisconnect(dst); });
            return b;
        }

        function badges(b) {
            if (b == null) return [];
            return (Array.isArray(b) ? b : [b]).map((one) => {
                const s = el("span", "btg-badge", one.label != null ? one.label : one);
                if (one.cls) String(one.cls).split(/\s+/).forEach((cl) => cl && s.classList.add(cl));
                return s;
            });
        }

        function toggle(set, key) { if (set.has(key)) set.delete(key); else set.add(key); saveSet(cfg.persistKey, set === st.collapsed ? "col" : "pin", set); render(); }

        // ── Barre « préparé → TAKE » ──
        function takeBar() {
            const bar = el("div", "btg-take");
            if (!st.prepared) { bar.classList.add("btg-hidden"); return bar; }
            const { src, dst } = st.prepared;
            const txt = cfg.preparedText ? cfg.preparedText(src, dst) : `${dst.label} ◄ ${src.label}`;
            bar.appendChild(el("span", "btg-take-txt", `${L.prepared} ${txt}`));
            const take = el("button", "btn btn-green", L.take);
            take.addEventListener("click", () => { const p = st.prepared; st.prepared = null; render(); if (cfg.onRoute) cfg.onRoute(p.src, p.dst); });
            const cancel = el("button", "btn", L.cancel);
            cancel.addEventListener("click", () => { st.prepared = null; render(); });
            bar.appendChild(take); bar.appendChild(cancel);
            return bar;
        }

        // ── Sélection de sources (clic sur en-tête ligne/groupe) → actions plugin ──
        function selToggle(keys, additive) {
            keys = (keys || []).filter(Boolean); if (!keys.length) return;
            if (additive) {                                   // Maj+clic : ajoute/retire, garde le reste
                if (keys.every((k) => st.selected.has(k))) keys.forEach((k) => st.selected.delete(k));
                else keys.forEach((k) => st.selected.add(k));
            } else {                                          // clic normal : sélection UNIQUE (re-clic = vide)
                const only = st.selected.size === keys.length && keys.every((k) => st.selected.has(k));
                st.selected.clear();
                if (!only) keys.forEach((k) => st.selected.add(k));
            }
            if (cfg.onSelectionChange) cfg.onSelectionChange([...st.selected]);
            render();
        }
        function clearSel() { if (!st.selected.size) return; st.selected.clear(); if (cfg.onSelectionChange) cfg.onSelectionChange([]); render(); }
        function selectedSources() { return sources.filter((s) => st.selected.has(s.key)); }

        function selBar() {
            const bar = el("div", "btg-sel");
            if (!st.selected.size) { bar.classList.add("btg-hidden"); return bar; }
            bar.appendChild(el("span", "btg-take-txt", `${st.selected.size} ${L.selected} · ${L.selHint}`));
            (cfg.selectionActions || []).forEach((a) => {
                const b = el("button", "btn" + (a.cls ? " " + a.cls : ""), a.label);
                if (a.title) b.title = a.title;
                b.addEventListener("click", () => a.onClick([...st.selected], selectedSources()));
                bar.appendChild(b);
            });
            const clear = el("button", "btn", L.clear);
            clear.addEventListener("click", clearSel);
            bar.appendChild(clear);
            return bar;
        }

        // ── Clic droit : sauter à une catégorie ──
        function attachJump(wrap, cols, rows) {
            wrap.addEventListener("contextmenu", (e) => {
                e.preventDefault(); closeJump();
                const menu = el("div", "btg-jump");
                const add = (title, anchors, o) => {
                    if (!anchors.length) return;
                    menu.appendChild(el("div", "btg-jump-h", title));
                    anchors.forEach((a) => { const b = el("button", "btg-jump-b", a.label); b.addEventListener("click", () => { a.el.scrollIntoView(o); closeJump(); }); menu.appendChild(b); });
                };
                add("↦ Destination", cols, { inline: "start", block: "nearest", behavior: "smooth" });
                add("↧ Source", rows, { block: "start", inline: "nearest", behavior: "smooth" });
                menu.style.left = e.clientX + "px"; menu.style.top = e.clientY + "px";
                document.body.appendChild(menu); st.jump = menu;
                setTimeout(() => document.addEventListener("click", closeJump, { once: true }), 0);
            });
        }
        function closeJump() { if (st.jump) { st.jump.remove(); st.jump = null; } }
    }

    // ── Persistance (localStorage) ──
    function loadSet(key, sub) {
        if (!key) return new Set();
        try { const p = JSON.parse(window.localStorage.getItem(key) || "{}"); return new Set(Array.isArray(p[sub]) ? p[sub] : []); } catch (e) { return new Set(); }
    }
    function saveSet(key, sub, set) {
        if (!key) return;
        try { const p = JSON.parse(window.localStorage.getItem(key) || "{}"); p[sub] = [...set]; window.localStorage.setItem(key, JSON.stringify(p)); } catch (e) { /* ignore */ }
    }
})();
