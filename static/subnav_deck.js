// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Chevron de la bande de sous-menu (`.subnav-deck`, cf. base.css), aligné sous l'entrée ALLUMÉE de
// la barre du haut : c'est lui qui dit de quelle page la bande est le sous-menu.
//
// Repris de Bobi.Studio (static/subnav_deck.js), sans la partie « ensemble à deux rangs »
// (`.navpair`) : Bobi.Tools n'a pas de sous-menu de page dans ses Réglages.
//
// Position MESURÉE et re-mesurée : la barre change de taille (mode condensé, passage sur deux
// lignes, chargement des polices). Un calcul unique au chargement serait faux.
(function () {
    function place() {
        const on = document.querySelector('.topnav a.active');
        document.querySelectorAll('.subnav-deck').forEach(deck => {
            let notch = deck.querySelector(':scope > .subnav-deck-notch');
            if (!notch) {
                notch = document.createElement('span');
                notch.className = 'subnav-deck-notch';
                notch.setAttribute('aria-hidden', 'true');
                deck.appendChild(notch);
            }
            if (!on) { notch.hidden = true; return; }
            const r = on.getBoundingClientRect(), d = deck.getBoundingClientRect();
            notch.style.left = Math.round(r.left + r.width / 2 - d.left) + 'px';
            notch.hidden = false;
        });
    }
    function start() {
        place();
        const nav = document.querySelector('.topnav');
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(place);
            if (nav) ro.observe(nav);
            document.querySelectorAll('.subnav-deck').forEach(d => ro.observe(d));
        } else {
            window.addEventListener('resize', place);
        }
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
    window.subnavDeckPlace = place;
})();
