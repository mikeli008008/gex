// GexLevels.js — paste into Tradovate Code Explorer (File > New)
// Auto-generated 2026-08-12 10:50 UTC. Add to chart with "Overlay on price pane".
const predef = require("./tools/predef");

class GexLevels {
    map() {
        return {
            callWall: 29904.50,
            gammaFlip: 29624.21,
            putWall: 29492.03,
            gxPOC: 29698.26,
            mag0: 29657.02,
            mag1: 29739.51,
            mag2: 29615.77,
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
