// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Socle JS de Bobi.Tools. Fournit le namespace window.BT : helpers génériques (toasts,
// fetch JSON) et surtout BT.openTool(type, hostEl) — la logique partagée qui monte l'UI
// d'un outil (servie par /api/tools/<type>/ui/*) dans le shell de test ET la vue focus.
// Chaque outil enregistre window.BTTools["<type>"] = { mount(el, ctx), unmount() }.
(function () {
    "use strict";
    const BT = window.BT = window.BT || {};
    window.BTTools = window.BTTools || {};

    BT.esc = (s) => String(s == null ? "" : s)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    // ── Toasts ───────────────────────────────────────────────
    function toastHost() {
        let h = document.getElementById("bt-toast-host");
        if (!h) {
            h = document.createElement("div");
            h.id = "bt-toast-host"; h.className = "toast-host"; h.setAttribute("role", "status");
            document.body.appendChild(h);
        }
        return h;
    }
    BT.toast = function (msg, kind = "info") {
        const el = document.createElement("div");
        el.className = "toast " + kind; el.textContent = msg;
        toastHost().appendChild(el);
        // Une erreur EXPLIQUE (ex. refus de droits : quel geste, quelle règle) : on lui laisse
        // le temps d'être lue — environ 60 ms par caractère, entre 4,2 et 12 s.
        const ms = kind === "error" ? Math.min(12000, Math.max(4200, String(msg).length * 60)) : 4200;
        setTimeout(() => el.remove(), ms);
    };

    // ── fetch JSON ───────────────────────────────────────────
    BT.fetchJSON = async function (url, opts = {}) {
        const o = Object.assign({ headers: {} }, opts);
        if (o.body !== undefined && typeof o.body !== "string") {
            o.headers["Content-Type"] = "application/json";
            o.body = JSON.stringify(o.body);
            o.method = o.method || "POST";
        }
        const r = await fetch(url, o);
        let data = null;
        try { data = await r.json(); } catch (e) { /* corps non-JSON */ }
        if (!r.ok) {
            const err = new Error((data && data.error) || ("HTTP " + r.status));
            err.status = r.status; err.data = data; throw err;
        }
        return data;
    };

    // ── SDK passé à mount(el, ctx) ───────────────────────────
    function toolCtx(type) {
        const base = "/api/tools/" + encodeURIComponent(type);
        // Un refus pour raison de DROITS (rôle ou règle de périmètre) est TOUJOURS montré à
        // l'exploitant, quel que soit le plugin : beaucoup n'affichent pas leurs erreurs, et
        // un geste qui « ne fait rien » sans dire pourquoi se lit comme une panne. Le message
        // vient du serveur (quel geste, où, quelle règle). L'erreur est relancée telle quelle ;
        // `err.rightsShown` permet au plugin d'éviter un second affichage.
        const call = (url, opts) => BT.fetchJSON(url, opts).catch(err => {
            if (err && err.data && err.data.rights) {
                BT.toast(err.message, "error");
                err.rightsShown = true;
            }
            throw err;
        });
        const api = (path, opts = {}) => call(base + "/" + String(path).replace(/^\//, ""), opts);
        const store = {
            list: (scope = "") => call(base + "/store?scope=" + encodeURIComponent(scope)),
            create: (name, value, opts = {}) => call(base + "/store", { body: { name, value, scope: opts.scope || "", unique_name: !!opts.unique } }),
            update: (id, patch) => call(base + "/store/" + id, { method: "PUT", body: patch }),
            remove: (id) => call(base + "/store/" + id, { method: "DELETE" }),
        };
        // Droits fins (cf. app/droits.py) : rights() dit quelles permissions de l'outil sont
        // accordées et lesquelles un périmètre restreint ; can() vérifie un lot de
        // {perm, resource} → [bool], pour GRISER ce que le serveur refuserait. Cosmétique :
        // le serveur revérifie chaque écriture.
        let rightsP = null;
        const rights = (fresh) => {
            if (!rightsP || fresh) rightsP = BT.fetchJSON(base + "/rights").catch(() => ({ admin: false, granted: [], restricted: [] }));
            return rightsP;
        };
        const can = async (checks) => {
            const list = Array.isArray(checks) ? checks : [checks];
            const r = await rights();
            if (r.admin) return list.map(() => true);
            // Sans périmètre sur la permission, le rôle suffit : pas d'aller-retour.
            const need = list.map((c, i) => [c, i]).filter(([c]) => r.granted.includes(c.perm) && r.restricted.includes(c.perm));
            const out = list.map(c => r.granted.includes(c.perm) && !r.restricted.includes(c.perm));
            if (need.length) {
                const res = await BT.fetchJSON(base + "/rights/check", { body: { checks: need.map(([c]) => c) } });
                need.forEach(([, i], k) => { out[i] = !!res.results[k]; });
            }
            return Array.isArray(checks) ? out : out[0];
        };
        return {
            type,
            runtime: (window.BT_TOOLS[type] || {}).runtime || "inprocess",
            toast: BT.toast,
            t: window.t,
            api, store, rights, can,
        };
    }

    // ── Chargement idempotent des assets UI d'un outil ───────
    function ensureAsset(type, kind) {
        const id = "bt-asset-" + type + "-" + kind;
        if (document.getElementById(id)) return Promise.resolve();
        return new Promise((res) => {
            let el;
            const src = "/api/tools/" + encodeURIComponent(type) + "/ui/" + kind + "?_=" + Date.now();
            if (kind === "js") { el = document.createElement("script"); el.src = src; }
            else { el = document.createElement("link"); el.rel = "stylesheet"; el.href = src; }
            el.id = id; el.onload = () => res(); el.onerror = () => res();
            document.head.appendChild(el);
        });
    }

    async function mountToolUI(type, mountEl) {
        try { window.BTTools[type] && window.BTTools[type].unmount && window.BTTools[type].unmount(); } catch (e) { }
        mountEl.innerHTML = '<div class="meta">' + BT.esc(window.t("js.loading")) + "</div>";
        let html = "";
        try { html = await (await fetch("/api/tools/" + encodeURIComponent(type) + "/ui/html?_=" + Date.now())).text(); }
        catch (e) { mountEl.innerHTML = '<div class="meta">' + BT.esc(window.t("js.ui_missing")) + "</div>"; return; }
        mountEl.innerHTML = html;
        await ensureAsset(type, "css");
        await ensureAsset(type, "js");
        const api = window.BTTools[type];
        if (api && typeof api.mount === "function") {
            try { api.mount(mountEl, toolCtx(type)); }
            catch (e) { console.error(e); BT.toast(window.t("js.mount_error") + " " + e.message, "error"); }
        } else {
            mountEl.innerHTML = '<div class="meta">' + BT.esc(window.t("js.no_ui")) + "</div>";
        }
    }

    BT.unmountTool = function (type) {
        try { window.BTTools[type] && window.BTTools[type].unmount && window.BTTools[type].unmount(); } catch (e) { }
    };

    // ── Cycle de vie d'un outil Docker (barre d'état) ────────
    async function lifecycle(type, action) {
        return BT.fetchJSON("/api/tools/" + encodeURIComponent(type) + "/lifecycle",
            action ? { body: { action } } : { method: "GET" });
    }

    function renderDockerBar(type, barEl, mountEl) {
        let started = false, pollTimer = null, versionVue = null, enConstruction = false;
        function setBar(st) {
            const running = st && st.running;
            if (!st || !st.available) {
                barEl.innerHTML = '<span class="bt-dot stopped"></span> ' + BT.esc(window.t("js.docker.unavailable"));
                return;
            }
            const label = running ? window.t("js.docker.running") : window.t("js.docker.stopped");
            const canUse = window.hasPerm && window.hasPerm("tools.use");
            const av = st.available_version || "", rv = st.running_version || "";
            const mismatch = running && st.up_to_date === false;
            // Construction d'image en arrière-plan (Migrer sans image locale) : on la MONTRE,
            // de son départ à son verdict. Avant, l'écran restait muet pendant des minutes
            // et l'échec n'apparaissait qu'à qui gardait la page ouverte jusqu'au bout.
            const b = st.build || {};
            const construit = b.state === "en_cours";
            const echec = b.state === "echec" && mismatch;
            const buildNote = construit
                ? '<span class="bt-migrate-note" style="font-size:.8em;color:var(--status-warning-fg)">⏳ construction de l\'image ' +
                  BT.esc(b.image || "") + " en cours depuis " + BT.esc((b.debut || "").slice(11, 16)) +
                  " — le conteneur sera recréé à la fin</span>"
                : echec
                ? '<details class="bt-migrate-note" style="font-size:.8em;color:var(--status-stopped-fg)"><summary>⚠ échec de la construction de ' +
                  BT.esc(b.image || "") + " — détail</summary><pre style=\"white-space:pre-wrap;max-width:70ch\">" + BT.esc(b.erreur || "") +
                  "</pre>Sans accès au registre Docker, importez l'image : Réglages → Déploiement → Images.</details>"
                : "";
            // Fin d'une construction : le conteneur a changé de version sous nos pieds.
            if (enConstruction && !construit) {
                if (b.state === "ok") BT.toast("Migré en v" + (st.running_version || st.available_version || ""), "info");
                else if (b.state === "echec") BT.toast("Échec de la construction de l'image — voir la barre d'état.", "error");
            }
            enConstruction = construit;
            if (started && versionVue && st.running_version && st.running_version !== versionVue) {
                started = false; BT.unmountTool(type);          // remonte l'UI sur le nouveau backend
            }
            if (st.running_version) versionVue = st.running_version;
            // Dépendance déclarée mais non installée. Docker crée à la volée un volume vide
            // pour un outil absent : sans cet avertissement, le consommateur démarrerait et
            // paraîtrait sain en ne voyant rien du tout.
            const missing = st.missing_requirements || [];
            const missNote = missing.length
                ? '<span class="bt-missing-req">⚠ ' + BT.esc(window.t("js.docker.missing_req")) + " : " +
                  missing.map((r) => BT.esc(r.type) + (r.reason ? " (" + BT.esc(r.reason) + ")" : "")).join(", ") +
                  "</span>"
                : "";
            barEl.innerHTML =
                '<span class="bt-dot ' + (running ? "running" : "stopped") + '"></span> ' +
                '<span class="bt-state">' + BT.esc(label) + (st.port ? ' · :' + st.port : "") +
                (av ? ' · v' + BT.esc(av) : "") + "</span>" + missNote +
                (mismatch && !construit ? '<span class="bt-migrate-note" style="font-size:.8em;color:var(--text-muted)">conteneur en v' +
                    BT.esc(rv) + " · v" + BT.esc(av) + " disponible</span>" : "") + buildNote +
                (canUse ? (
                    (mismatch && !construit ? '<button class="btn btn-orange bt-life" data-a="migrate">⬆ Migrer</button>' : "") +
                    (running
                        ? '<button class="btn btn-red bt-life" data-a="stop">' + BT.esc(window.t("js.docker.stop")) + "</button>"
                        : '<button class="btn btn-green bt-life" data-a="start">' + BT.esc(window.t("js.docker.start")) + "</button>")
                ) : "");
            barEl.querySelectorAll(".bt-life").forEach((b) => b.onclick = () => act(b.dataset.a, b));
            // (Re)monte l'UI quand l'outil passe à running ; démonte sinon.
            if (running && !started) { started = true; mountToolUI(type, mountEl); }
            if (!running && started) { started = false; BT.unmountTool(type); mountEl.innerHTML = '<div class="tp-mount empty"><div>' + BT.esc(window.t("js.docker.start_hint")) + "</div></div>"; }
        }
        async function act(action, btn) {
            if (action === "migrate" && !confirm("Migrer l'outil vers la version disponible ?\nLe conteneur sera reconstruit si besoin puis recréé (quelques secondes).")) return;
            if (btn) btn.disabled = true;
            BT.toast(action === "migrate" ? "Migration en cours…" :
                window.t("js.docker." + (action === "start" ? "starting" : "stopping")), "info");
            try {
                const st = await lifecycle(type, action);
                const enFond = action === "migrate" && st.build && st.build.state === "en_cours";
                if (action === "migrate" && !enFond) { started = false; BT.unmountTool(type); }   // remonte sur le nouveau backend
                setBar(st);
                if (enFond) BT.toast("Image à construire : la construction tourne en arrière-plan, la barre d'état suit son avancement.", "info");
                else if (action === "migrate") BT.toast("Migré en v" + (st.available_version || ""), "info");
            }
            catch (e) { BT.toast(e.message, "error"); if (btn) btn.disabled = false; }
        }
        async function poll() {
            try { setBar(await lifecycle(type, null)); } catch (e) { }
        }
        poll(); pollTimer = setInterval(poll, 5000);
        return () => { if (pollTimer) clearInterval(pollTimer); };
    }

    // ── Point d'entrée unique : monte un outil dans hostEl ───
    // Construit [barre d'état Docker?] + [zone de montage] et lance le bon flux.
    BT.openTool = function (type, hostEl) {
        hostEl.innerHTML = '<div class="bt-tool-bar" hidden></div><div class="bt-tool-mount"></div>';
        const bar = hostEl.querySelector(".bt-tool-bar");
        const mount = hostEl.querySelector(".bt-tool-mount");
        const runtime = (window.BT_TOOLS[type] || {}).runtime || "inprocess";
        if (runtime === "docker") {
            bar.hidden = false;
            hostEl._stopPoll = renderDockerBar(type, bar, mount);
        } else {
            mountToolUI(type, mount);
        }
    };
    BT.closeTool = function (type, hostEl) {
        if (hostEl && hostEl._stopPoll) hostEl._stopPoll();
        BT.unmountTool(type);
    };
})();
