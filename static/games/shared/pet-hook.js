/* ============================================================================
   MusePet — the pet-attachment contract for MuseFM games.
   ----------------------------------------------------------------------------
   WHAT THIS IS:
   Every game in the games room renders the player's ACTUAL pet through this
   hook instead of drawing its own creature. One contract, every game.

   HOW A GAME USES IT:
     <script src="../shared/pet-hook.js"></script>
     ...
     var pet = MusePet.get();                    // pet data object
     var svg = MusePet.renderSVG(pet, 'happy');  // SVG string, mood-aware
     // draw the SVG (e.g. rasterize to an Image and drawImage it on canvas,
     // or inject it into the DOM)

   WHERE THE DATA COMES FROM (priority order in get()):
     1. URL param  ?pet=  — JSON-encoded pet object. The site injects the
        signed-in member's real pet this way when launching a game.
     2. localStorage 'muse_pet_v1' — JSON pet object. Set by the site shell
        (or a previous session) so the pet survives across games.
     3. Built-in default — a teal blob in the pet-cove style, so every game
        works standalone with zero setup.

   PET OBJECT SHAPE (all fields optional; missing fields fall back to default):
     {
       name:     'Pebble',     // display name
       species:  'blob',       // species id (reserved for future art variants)
       colorHi:  '#b9fff1',    // body gradient highlight
       colorMid: '#4fe3c8',    // body gradient mid
       colorLo:  '#0e9c8f',    // body gradient edge
       cheek:    '#ffd9e8'     // cheek blush color
     }

   MOODS for renderSVG(pet, mood):
     'idle' | 'happy' | 'watching' | 'excited' | 'sparkle' | 'dash'
     | 'sad' | 'blink'   (unknown moods fall back to 'idle')

   SITE INTEGRATION NOTE:
   When these games go on the site, the member's real pet data gets injected
   through this hook (option 1 or 2 above). Game code must NEVER hardcode a
   pet's look; it always goes through MusePet.get() + MusePet.renderSVG().
   Per the room rule: game collections and progress must persist to member
   profiles server-side; this hook is the client side of that same promise
   for the pet itself.
   ============================================================================ */
(function(){
'use strict';

var LS_KEY = 'muse_pet_v1';

function defaultPet(){
  return {
    name: 'Pebble',
    species: 'blob',
    colorHi: '#b9fff1',
    colorMid: '#4fe3c8',
    colorLo: '#0e9c8f',
    cheek: '#ffd9e8'
  };
}

function normalize(p){
  var d = defaultPet();
  if (!p || typeof p !== 'object') return d;
  return {
    name: (typeof p.name === 'string' && p.name) || d.name,
    species: (typeof p.species === 'string' && p.species) || d.species,
    colorHi: p.colorHi || d.colorHi,
    colorMid: p.colorMid || d.colorMid,
    colorLo: p.colorLo || d.colorLo,
    cheek: p.cheek || d.cheek
  };
}

function get(){
  try {
    var q = new URLSearchParams(window.location.search).get('pet');
    if (q){
      var qp = JSON.parse(q);
      if (qp && typeof qp === 'object') return normalize(qp);
    }
  } catch(e){}
  try {
    var s = window.localStorage.getItem(LS_KEY);
    if (s){
      var sp = JSON.parse(s);
      if (sp && typeof sp === 'object') return normalize(sp);
    }
  } catch(e){}
  return defaultPet();
}

/* Face variations per mood. Body matches the pet-cove blob style:
   teal radial-gradient blob, dark eyes, blush cheeks, gloss highlight. */
function faceFor(mood, dark){
  switch (mood){
    case 'happy':
    case 'excited':
    case 'sparkle':
    case 'dash':
      return '' +
        '<path d="M62 104 Q76 90 90 104" stroke="'+dark+'" stroke-width="5" fill="none" stroke-linecap="round"/>' +
        '<path d="M110 104 Q124 90 138 104" stroke="'+dark+'" stroke-width="5" fill="none" stroke-linecap="round"/>' +
        '<path d="M86 124 Q100 146 114 124 Q100 134 86 124 Z" fill="'+dark+'"/>';
    case 'sad':
      return '' +
        '<ellipse cx="76" cy="106" rx="11" ry="8" fill="'+dark+'"/>' +
        '<ellipse cx="124" cy="106" rx="11" ry="8" fill="'+dark+'"/>' +
        '<circle cx="80" cy="103" r="3.5" fill="#ffffff"/>' +
        '<circle cx="128" cy="103" r="3.5" fill="#ffffff"/>' +
        '<path d="M88 136 Q100 126 112 136" stroke="'+dark+'" stroke-width="4" fill="none" stroke-linecap="round"/>';
    case 'blink':
      return '' +
        '<path d="M64 102 L88 102" stroke="'+dark+'" stroke-width="5" stroke-linecap="round"/>' +
        '<path d="M112 102 L136 102" stroke="'+dark+'" stroke-width="5" stroke-linecap="round"/>' +
        '<path d="M88 128 Q100 138 112 128" stroke="'+dark+'" stroke-width="4" fill="none" stroke-linecap="round"/>';
    default: /* idle, watching */
      return '' +
        '<circle cx="76" cy="102" r="13" fill="'+dark+'"/>' +
        '<circle cx="124" cy="102" r="13" fill="'+dark+'"/>' +
        '<circle cx="80" cy="98" r="4.5" fill="#ffffff"/>' +
        '<circle cx="128" cy="98" r="4.5" fill="#ffffff"/>' +
        '<path d="M88 128 Q100 138 112 128" stroke="'+dark+'" stroke-width="4" fill="none" stroke-linecap="round"/>';
  }
}

function renderSVG(pet, mood){
  pet = normalize(pet);
  var dark = '#06343b';
  var face = faceFor(mood || 'idle', dark);
  return '' +
  '<svg width="200" height="200" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">' +
    '<defs>' +
      '<radialGradient id="mpb" cx="40%" cy="32%" r="75%">' +
        '<stop offset="0%" stop-color="'+pet.colorHi+'"/>' +
        '<stop offset="45%" stop-color="'+pet.colorMid+'"/>' +
        '<stop offset="100%" stop-color="'+pet.colorLo+'"/>' +
      '</radialGradient>' +
      '<radialGradient id="mpc" cx="50%" cy="50%" r="50%">' +
        '<stop offset="0%" stop-color="'+pet.cheek+'" stop-opacity="0.85"/>' +
        '<stop offset="100%" stop-color="'+pet.cheek+'" stop-opacity="0"/>' +
      '</radialGradient>' +
    '</defs>' +
    '<ellipse cx="100" cy="112" rx="72" ry="64" fill="url(#mpb)"/>' +
    '<ellipse cx="70" cy="72" rx="26" ry="16" fill="#ffffff" opacity="0.5" transform="rotate(-25 70 72)"/>' +
    '<circle cx="150" cy="50" r="6" fill="#ffffff" opacity="0.65"/>' +
    '<circle cx="160" cy="66" r="3.5" fill="#ffffff" opacity="0.55"/>' +
    '<g>' + face + '</g>' +
    '<ellipse cx="62" cy="122" rx="12" ry="8" fill="url(#mpc)"/>' +
    '<ellipse cx="138" cy="122" rx="12" ry="8" fill="url(#mpc)"/>' +
  '</svg>';
}

window.MusePet = {
  get: get,
  renderSVG: renderSVG,
  defaultPet: defaultPet,
  LS_KEY: LS_KEY
};
})();
