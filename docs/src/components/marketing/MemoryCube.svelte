<script lang="ts">
  import { onMount } from 'svelte';

  let host: HTMLDivElement;
  let canvas: HTMLCanvasElement;
  let ready = false;

  onMount(() => {
    let frame = 0;
    let disposed = false;
    let visible = true;
    let pointerX = 0;
    let pointerY = 0;

    const render = async () => {
      const THREE = await import('three');
      const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js');
      if (disposed) return;

      const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.12;
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 100);
      camera.position.set(5.8, 4.9, 7.5);
      camera.lookAt(0, 0, 0);

      scene.add(new THREE.HemisphereLight(0xe7edff, 0x8c6f51, 2.5));
      const key = new THREE.DirectionalLight(0xffffff, 5.2);
      key.position.set(-5, 7, 6);
      key.castShadow = true;
      key.shadow.mapSize.set(1024, 1024);
      scene.add(key);
      const rim = new THREE.DirectionalLight(0x8ea9ff, 3.4);
      rim.position.set(6, 2, -4);
      scene.add(rim);

      const root = new THREE.Group();
      const model = (await new GLTFLoader().loadAsync('/aizk-memory-cube.glb')).scene;
      if (disposed) {
        renderer.dispose();
        return;
      }
      const bounds = new THREE.Box3().setFromObject(model);
      const center = bounds.getCenter(new THREE.Vector3());
      model.position.sub(center);
      model.traverse((item) => {
        if (item instanceof THREE.Mesh) {
          item.castShadow = true;
          item.receiveShadow = true;
        }
      });
      root.add(model);
      root.rotation.set(0.08, -0.55, -0.03);
      scene.add(root);

      const floor = new THREE.Mesh(
        new THREE.CircleGeometry(3.1, 64),
        new THREE.ShadowMaterial({ color: 0x17223b, opacity: 0.14 })
      );
      floor.rotation.x = -Math.PI / 2;
      floor.position.y = -1.73;
      floor.receiveShadow = true;
      scene.add(floor);

      const resize = () => {
        const width = Math.max(host.clientWidth, 1);
        const height = Math.max(host.clientHeight, 1);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
      };

      const scrollProgress = () => {
        const rect = host.getBoundingClientRect();
        return THREE.MathUtils.clamp(
          (window.innerHeight - rect.top) / (window.innerHeight + rect.height),
          0,
          1
        );
      };

      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const tick = () => {
        const progress = reducedMotion ? 0.48 : scrollProgress();
        const targetX = 0.02 + progress * 0.2 + pointerY * 0.08;
        const targetY = -0.72 + progress * 1.08 + pointerX * 0.2;
        root.rotation.x = THREE.MathUtils.lerp(root.rotation.x, targetX, 0.07);
        root.rotation.y = THREE.MathUtils.lerp(root.rotation.y, targetY, 0.07);
        root.rotation.z = THREE.MathUtils.lerp(root.rotation.z, pointerX * -0.04, 0.06);
        root.position.y = THREE.MathUtils.lerp(root.position.y, (progress - 0.5) * 0.22, 0.06);
        host.dataset.scrollProgress = progress.toFixed(3);
        host.dataset.rotationY = root.rotation.y.toFixed(4);
        renderer.render(scene, camera);
        if (!ready) ready = true;
        frame = visible && !reducedMotion ? requestAnimationFrame(tick) : 0;
      };

      const move = (event: PointerEvent) => {
        const rect = host.getBoundingClientRect();
        pointerX = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
        pointerY = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
      };
      const leave = () => {
        pointerX = 0;
        pointerY = 0;
      };
      const resizeObserver = new ResizeObserver(() => {
        resize();
        if (!frame) tick();
      });
      const visibilityObserver = new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting;
        if (visible && !frame) tick();
      });
      resizeObserver.observe(host);
      visibilityObserver.observe(host);
      host.addEventListener('pointermove', move);
      host.addEventListener('pointerleave', leave);
      resize();
      tick();

      return () => {
        resizeObserver.disconnect();
        visibilityObserver.disconnect();
        host.removeEventListener('pointermove', move);
        host.removeEventListener('pointerleave', leave);
        cancelAnimationFrame(frame);
        renderer.dispose();
      };
    };

    let release: (() => void) | undefined;
    void render()
      .then((cleanup) => {
        if (cleanup) release = cleanup;
      })
      .catch(() => {
        ready = false;
      });
    return () => {
      disposed = true;
      release?.();
    };
  });
</script>

<div bind:this={host} class="relative size-full" data-memory-cube>
  <img
    src="/brain-box.webp"
    alt="A memory block formed from interlocking brain folds"
    class="absolute inset-0 size-full object-cover transition-opacity duration-700 {ready ? 'opacity-0' : 'opacity-100'}"
    width="1024"
    height="1024"
    fetchpriority="high"
  />
  <canvas
    bind:this={canvas}
    class="absolute inset-0 size-full touch-none transition-opacity duration-700 {ready ? 'opacity-100' : 'opacity-0'}"
    aria-hidden="true"
  ></canvas>
</div>
