// GexLevels.js — paste into Tradovate Code Explorer (File > New)
// Auto-generated 2026-11-18 18:29 UTC. Add to chart with "Overlay on price pane".
const predef = require("./tools/predef");

class GexLevels {
    map() {
        return {
            callWall: 27410.49,
            gammaFlip: 27114.16,
            putWall: 26832.56,
            gxPOC: 27245.36,
            mag0: 27038.96,
            mag1: 26997.68,
            mag2: 27369.21,
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
