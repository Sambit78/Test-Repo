/* Clean Home: an ad-free, Apple TV-style launcher for Samsung Tizen TVs.
 * Shows only the apps in FAVOURITES, in that order, using each app's own
 * icon from the TV. Arrow keys move, OK opens the app. */

// Edit this list to change which apps appear and in what order. Names are
// matched loosely against the app names installed on the TV.
var FAVOURITES = [
    "YouTube", "Netflix", "Prime Video", "Disney+", "BINGE",
    "Kayo", "7plus", "Apple TV", "Optus Sport", "YuppTV"
];

var COLUMNS = 5;
var KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13, BACK: 10009 };

var tiles = [];
var focus = 0;

function normalise(name) {
    return String(name || "").toLowerCase().replace(/[^a-z0-9+]/g, "");
}

function pickFavourites(installed) {
    var picked = [];
    FAVOURITES.forEach(function (wanted) {
        var key = normalise(wanted);
        for (var i = 0; i < installed.length; i++) {
            var app = installed[i];
            var name = normalise(app.name);
            if (name === key || name.indexOf(key) === 0) {
                if (picked.indexOf(app) === -1) picked.push(app);
                return;
            }
        }
    });
    return picked;
}

function render(apps) {
    var grid = document.getElementById("grid");
    grid.innerHTML = "";
    tiles = apps.map(function (app) {
        var tile = document.createElement("div");
        tile.className = "tile";
        var label = document.createElement("div");
        label.className = "label";
        label.textContent = app.name;
        if (app.iconPath) {
            var img = document.createElement("img");
            img.src = app.iconPath;
            img.onerror = function () { tile.replaceChild(label, img); };
            tile.appendChild(img);
        } else {
            tile.appendChild(label);
        }
        grid.appendChild(tile);
        return { el: tile, app: app };
    });
    if (!tiles.length) {
        message("None of the favourite apps were found. Edit FAVOURITES in main.js.");
        return;
    }
    setFocus(0);
}

function setFocus(i) {
    if (i < 0 || i >= tiles.length) return;
    if (tiles[focus]) tiles[focus].el.classList.remove("focused");
    focus = i;
    var t = tiles[focus];
    t.el.classList.add("focused");
    document.getElementById("focused-name").textContent = t.app.name;
    document.getElementById("backdrop").style.backgroundImage =
        t.app.iconPath ? "url('" + t.app.iconPath + "')" : "none";
}

function open(app) {
    message("Opening " + app.name + "…");
    tizen.application.launch(app.id,
        function () { message(""); },
        function (e) { message("Couldn't open " + app.name + ": " + e.message); });
}

function message(text) {
    document.getElementById("message").textContent = text;
}

function tick() {
    var now = new Date();
    var h = now.getHours();
    var m = ("0" + now.getMinutes()).slice(-2);
    document.getElementById("clock").textContent = ((h + 11) % 12 + 1) + ":" + m;
    document.getElementById("greeting").textContent =
        h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

document.addEventListener("keydown", function (e) {
    switch (e.keyCode) {
        case KEY.LEFT: setFocus(focus - 1); break;
        case KEY.RIGHT: setFocus(focus + 1); break;
        case KEY.UP: setFocus(focus - COLUMNS); break;
        case KEY.DOWN: setFocus(Math.min(focus + COLUMNS, tiles.length - 1)); break;
        case KEY.ENTER: if (tiles[focus]) open(tiles[focus].app); break;
        case KEY.BACK: break; // stay on the launcher
        default: return;
    }
    e.preventDefault();
});

window.onload = function () {
    tick();
    setInterval(tick, 10000);
    if (!window.tizen) {
        message("This page runs on a Samsung TV.");
        return;
    }
    tizen.application.getAppsInfo(
        function (installed) { render(pickFavourites(installed)); },
        function (e) { message("Couldn't read the app list: " + e.message); });
};
