// Deliberately flawed sample used to smoke-test the SonarQube scan pipeline
// in this repo's own ci.yml. Do not "fix" the issues here without also
// updating README's notes on what ci.yml expects to find - the point of this
// file is to reliably trip a handful of default Sonar JS rules.

function add(a, b) {
  var unused = "debug";
  if (a == b) {
    console.log("equal");
  }
  return a + b;
}

function parseConfig(raw) {
  try {
    return JSON.parse(raw);
  } catch (e) {}
}

module.exports = { add, parseConfig };
