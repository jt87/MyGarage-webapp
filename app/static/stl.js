// Minimal STL viewer (three.js, vendored in static/vendor, loaded only on STL pages).
import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const el = document.getElementById("stl");
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
const scene = new THREE.Scene();
scene.background = new THREE.Color(dark ? 0x1d1f23 : 0xf9f7f4);
const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1e6);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2.2));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
scene.add(sun);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

function resize() {
  const { clientWidth: w, clientHeight: h } = el;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

new STLLoader().load(el.dataset.src, (geo) => {
  geo.computeVertexNormals();
  geo.center();
  geo.computeBoundingSphere();
  const r = geo.boundingSphere.radius || 1;
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0xe8590c, metalness: 0.1, roughness: 0.6 }));
  mesh.rotation.x = -Math.PI / 2;
  scene.add(mesh);
  camera.position.set(r * 1.6, r * 1.2, r * 2.0);
  sun.position.set(r * 2, r * 3, r * 2);
  camera.near = r / 100; camera.far = r * 100; camera.updateProjectionMatrix();
  el.replaceChildren(renderer.domElement);
  resize();
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
}, undefined, () => { el.innerHTML = '<p class="muted">' + (navigator.onLine ? "Could not load this model." : "This model isn't saved on this device yet. Open it once while you're online to keep it.") + '</p>'; });

addEventListener("resize", resize);
