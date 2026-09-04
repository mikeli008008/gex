// GexLevels.js — paste into Tradovate Code Explorer (File > New)
// Auto-generated 2026-09-04 13:50 UTC. Add to chart with "Overlay on price pane".
const predef = require("./tools/predef");

class GexLevels {
    map() {
        return {
            callWall: 29818.63,
            gammaFlip: 29454.18,
            putWall: 29530.73,
            gxPOC: 29612.99,
            mag0: 29407.34,
            mag1: 29695.25,
            mag2: 29571.86,
        };
    }
}

module.exports = {
    name: "GexLevels",
    description: "GEX Levels (NQ) — auto-emit",
    calculator: GexLevels,
    tags: ["GEX"],
    params: {},
    plots: {
        callWall: { title: "Call Wall" },
        gammaFlip: { title: "Gamma Flip" },
        putWall: { title: "Put Wall" },
        gxPOC: { title: "GX POC" },
        mag0: { title: "Magnet 1" },
        mag1: { title: "Magnet 2" },
        mag2: { title: "Magnet 3" }
    },
    schemeStyles: {
        dark: {
            callWall: { color: "red", lineWidth: 1, lineStyle: 1 },
            gammaFlip: { color: "yellow", lineWidth: 1, lineStyle: 1 },
            putWall: { color: "lime", lineWidth: 1, lineStyle: 1 },
            gxPOC: { color: "orange", lineWidth: 1, lineStyle: 1 },
            mag0: { color: "aqua", lineWidth: 1, lineStyle: 1 },
            mag1: { color: "aqua", lineWidth: 1, lineStyle: 1 },
            mag2: { color: "aqua", lineWidth: 1, lineStyle: 1 }
        }
    }
};
