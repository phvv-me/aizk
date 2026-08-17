from pathlib import Path

import bpy  # pyrefly: ignore [missing-import]
from mathutils import Vector  # pyrefly: ignore [missing-import]


class AizkBrandScene:
    """Build and render the procedural AIZK memory cube."""

    def __init__(self) -> None:
        self.output = Path(__file__).parents[1] / "src" / "assets" / "brand-3d"
        self.render_output = self.output / "renders"
        self.layer_output = self.output / "icon-composer"
        self.collections: dict[str, bpy.types.Collection] = {}
        self.materials: dict[str, bpy.types.Material] = {}
        self.camera: bpy.types.Object | None = None

    def render_all(self) -> None:
        """Create the scene, save its geometry, and render every deliverable."""
        self.render_output.mkdir(parents=True, exist_ok=True)
        self.layer_output.mkdir(parents=True, exist_ok=True)
        self.__reset()
        self.__configure_scene()
        self.__create_collections()
        self.__create_materials()
        self.__build_memory_cube()
        self.__build_stage()
        self.__save_sources()
        self.__render_material_studies()
        self.__render_icon_composer_layers()

    def __reset(self) -> None:
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        for collection in list(bpy.data.collections):
            bpy.data.collections.remove(collection)
        for material in list(bpy.data.materials):
            bpy.data.materials.remove(material)

    def __configure_scene(self) -> None:
        scene = bpy.context.scene
        scene.render.engine = "BLENDER_EEVEE_NEXT"
        scene.render.resolution_x = 1024
        scene.render.resolution_y = 1024
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA"
        scene.render.image_settings.color_depth = "8"
        scene.render.film_transparent = False
        scene.render.image_settings.color_mode = "RGBA"
        scene.view_settings.look = "AgX - Medium High Contrast"
        scene.render.resolution_percentage = 100
        scene.world.color = (0.035, 0.025, 0.06)

    def __create_collections(self) -> None:
        scene = bpy.context.scene
        for name in ("Base", "FoldsA", "FoldsB", "Node", "Stage"):
            collection = bpy.data.collections.new(name)
            scene.collection.children.link(collection)
            self.collections[name] = collection

    def __create_materials(self) -> None:
        self.materials = {
            "ceramic": self.__procedural_material(
                "Ceramic folds",
                ((0.035, 0.08, 0.62, 1), (0.22, 0.2, 0.72, 1), (0.82, 0.74, 0.58, 1)),
                0.0,
                0.4,
                0.18,
            ),
            "resin": self.__procedural_material(
                "Resin folds",
                ((0.045, 0.02, 0.3, 1), (0.24, 0.1, 0.7, 1), (0.44, 0.32, 0.78, 1)),
                0.0,
                0.22,
                0.42,
            ),
            "metal": self.__procedural_material(
                "Metal folds",
                ((0.025, 0.08, 0.48, 1), (0.22, 0.08, 0.7, 1), (0.58, 0.48, 0.9, 1)),
                0.72,
                0.2,
                0.48,
            ),
            "coral": self.__material("Source coral", (1.0, 0.105, 0.045, 1), 0.05, 0.18, 0.5),
            "ground": self.__material("Warm paper", (0.88, 0.8, 0.66, 1), 0.0, 0.52, 0.08),
            "flat_base": self.__emission("Flat base", (0.04, 0.12, 0.82, 1)),
            "flat_a": self.__masked_emission("Flat periwinkle", (0.31, 0.34, 0.95, 1), 0.34, 0.66),
            "flat_b": self.__masked_emission("Flat ivory", (0.95, 0.9, 0.78, 1), 0.66, 0.88),
            "flat_combined": self.__procedural_emission(
                "Flat combined",
                ((0.04, 0.12, 0.82, 1), (0.31, 0.34, 0.95, 1), (0.95, 0.9, 0.78, 1)),
            ),
            "flat_coral": self.__emission("Flat coral", (1.0, 0.13, 0.06, 1)),
        }

    def __procedural_material(
        self,
        name: str,
        colors: tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ],
        metallic: float,
        roughness: float,
        coat: float,
    ) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        field = self.__fold_field(material)
        color = self.__palette(material, field, colors, "B_SPLINE")
        height = self.__palette(
            material,
            field,
            ((0.08, 0.08, 0.08, 1), (0.48, 0.48, 0.48, 1), (0.9, 0.9, 0.9, 1)),
            "B_SPLINE",
        )
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.42
        bump.inputs["Distance"].default_value = 0.16
        material.node_tree.links.new(height, bump.inputs["Height"])

        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Metallic"].default_value = metallic
        shader.inputs["Roughness"].default_value = roughness
        shader.inputs["Coat Weight"].default_value = coat
        shader.inputs["Coat Roughness"].default_value = min(roughness * 0.7, 0.3)
        material.node_tree.links.new(color, shader.inputs["Base Color"])
        material.node_tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
        output = nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        return material

    def __procedural_emission(
        self,
        name: str,
        colors: tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ],
    ) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        field = self.__fold_field(material)
        color = self.__palette(material, field, colors, "CONSTANT")
        emission = nodes.new("ShaderNodeEmission")
        material.node_tree.links.new(color, emission.inputs["Color"])
        output = nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
        return material

    def __masked_emission(
        self,
        name: str,
        color: tuple[float, float, float, float],
        lower: float,
        upper: float,
    ) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        field = self.__fold_field(material)
        above = nodes.new("ShaderNodeMath")
        above.operation = "GREATER_THAN"
        above.inputs[1].default_value = lower
        below = nodes.new("ShaderNodeMath")
        below.operation = "LESS_THAN"
        below.inputs[1].default_value = upper
        mask = nodes.new("ShaderNodeMath")
        mask.operation = "MULTIPLY"
        material.node_tree.links.new(field, above.inputs[0])
        material.node_tree.links.new(field, below.inputs[0])
        material.node_tree.links.new(above.outputs[0], mask.inputs[0])
        material.node_tree.links.new(below.outputs[0], mask.inputs[1])

        transparent = nodes.new("ShaderNodeBsdfTransparent")
        emission = nodes.new("ShaderNodeEmission")
        emission.inputs["Color"].default_value = color
        mix = nodes.new("ShaderNodeMixShader")
        material.node_tree.links.new(mask.outputs[0], mix.inputs[0])
        material.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
        material.node_tree.links.new(emission.outputs[0], mix.inputs[2])
        output = nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(mix.outputs[0], output.inputs["Surface"])
        return material

    def __fold_field(self, material: bpy.types.Material) -> bpy.types.NodeSocket:
        nodes = material.node_tree.nodes
        texture = nodes.new("ShaderNodeTexCoord")
        noise = nodes.new("ShaderNodeTexNoise")
        noise.noise_dimensions = "3D"
        noise.inputs["Scale"].default_value = 1.28
        noise.inputs["Detail"].default_value = 3.2
        noise.inputs["Roughness"].default_value = 0.62
        noise.inputs["Distortion"].default_value = 0.34
        bands = nodes.new("ShaderNodeMath")
        bands.operation = "MULTIPLY"
        bands.inputs[1].default_value = 3.1
        fraction = nodes.new("ShaderNodeMath")
        fraction.operation = "FRACT"
        material.node_tree.links.new(texture.outputs["Generated"], noise.inputs["Vector"])
        material.node_tree.links.new(noise.outputs["Fac"], bands.inputs[0])
        material.node_tree.links.new(bands.outputs[0], fraction.inputs[0])
        return fraction.outputs[0]

    def __palette(
        self,
        material: bpy.types.Material,
        field: bpy.types.NodeSocket,
        colors: tuple[
            tuple[float, float, float, float],
            tuple[float, float, float, float],
            tuple[float, float, float, float],
        ],
        interpolation: str,
    ) -> bpy.types.NodeSocket:
        ramp = material.node_tree.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.interpolation = interpolation
        stops = (
            (0.0, colors[0]),
            (0.3, colors[0]),
            (0.34, colors[1]),
            (0.62, colors[1]),
            (0.66, colors[2]),
            (0.84, colors[2]),
            (0.88, colors[0]),
            (1.0, colors[0]),
        )
        ramp.color_ramp.elements.remove(ramp.color_ramp.elements[1])
        first = ramp.color_ramp.elements[0]
        first.position, first.color = stops[0]
        for position, color in stops[1:]:
            element = ramp.color_ramp.elements.new(position)
            element.color = color
        material.node_tree.links.new(field, ramp.inputs["Fac"])
        return ramp.outputs["Color"]

    def __material(
        self,
        name: str,
        color: tuple[float, float, float, float],
        metallic: float,
        roughness: float,
        coat: float,
        subsurface: float = 0.0,
    ) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        shader = nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = color
        shader.inputs["Metallic"].default_value = metallic
        shader.inputs["Roughness"].default_value = roughness
        shader.inputs["Coat Weight"].default_value = coat
        shader.inputs["Coat Roughness"].default_value = min(roughness * 0.7, 0.3)
        shader.inputs["Subsurface Weight"].default_value = subsurface

        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 8.0 if metallic < 0.5 else 42.0
        noise.inputs["Detail"].default_value = 3.0
        noise.inputs["Roughness"].default_value = 0.62
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.08 if metallic < 0.5 else 0.16
        bump.inputs["Distance"].default_value = 0.035
        material.node_tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        material.node_tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
        return material

    def __emission(
        self, name: str, color: tuple[float, float, float, float]
    ) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        emission = nodes.new("ShaderNodeEmission")
        emission.inputs["Color"].default_value = color
        emission.inputs["Strength"].default_value = 1.0
        output = nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
        return material

    def __build_memory_cube(self) -> None:
        base = self.__rounded_cube("Memory cube", 3.0, 0.3, self.materials["ceramic"])
        self.__move_to_collection(base, "Base")
        folds_a = self.__rounded_cube("Periwinkle mask", 3.0, 0.3, self.materials["flat_a"])
        self.__move_to_collection(folds_a, "FoldsA")
        folds_b = self.__rounded_cube("Ivory mask", 3.0, 0.3, self.materials["flat_b"])
        self.__move_to_collection(folds_b, "FoldsB")

        bpy.ops.mesh.primitive_uv_sphere_add(
            segments=64, ring_count=32, radius=0.17, location=(-0.4, -1.63, 0.12)
        )
        node = bpy.context.object
        node.name = "Source node"
        node.data.materials.append(self.materials["coral"])
        self.__move_to_collection(node, "Node")

    def __rounded_cube(
        self, name: str, size: float, bevel: float, material: bpy.types.Material
    ) -> bpy.types.Object:
        bpy.ops.mesh.primitive_cube_add(size=size)
        cube = bpy.context.object
        cube.name = name
        modifier = cube.modifiers.new("Rounded edges", "BEVEL")
        modifier.width = bevel
        modifier.segments = 10
        modifier.limit_method = "ANGLE"
        bpy.context.view_layer.objects.active = cube
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        bpy.ops.object.shade_smooth_by_angle()
        cube.data.materials.append(material)
        return cube

    def __build_stage(self) -> None:
        bpy.ops.mesh.primitive_plane_add(size=30, location=(0, 0, -1.82))
        ground = bpy.context.object
        ground.name = "Ground"
        ground.data.materials.append(self.materials["ground"])
        self.__move_to_collection(ground, "Stage")

        self.camera = self.__camera((5.8, -7.2, 5.4), 5.7)
        self.__area_light("Key", (-4.8, -5.0, 7.5), 1050, 5.0)
        self.__area_light("Fill", (5.2, -2.0, 4.0), 680, 4.0)
        self.__area_light("Rim", (2.0, 5.0, 7.0), 920, 3.5)

    def __camera(self, location: tuple[float, float, float], scale: float) -> bpy.types.Object:
        camera_data = bpy.data.cameras.new("Camera")
        camera = bpy.data.objects.new("Camera", camera_data)
        self.collections["Stage"].objects.link(camera)
        camera.location = location
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = scale
        camera.rotation_euler = self.__look_at(camera.location, Vector((0, 0, 0)))
        bpy.context.scene.camera = camera
        return camera

    def __area_light(
        self,
        name: str,
        location: tuple[float, float, float],
        energy: float,
        size: float,
    ) -> None:
        light_data = bpy.data.lights.new(name, "AREA")
        light_data.energy = energy
        light_data.shape = "DISK"
        light_data.size = size
        light = bpy.data.objects.new(name, light_data)
        self.collections["Stage"].objects.link(light)
        light.location = location
        light.rotation_euler = self.__look_at(light.location, Vector((0, 0, 0)))

    def __look_at(self, origin: Vector, target: Vector) -> tuple[float, float, float]:
        return (target - origin).to_track_quat("-Z", "Y").to_euler()

    def __move_to_collection(self, item: bpy.types.Object, collection: str) -> None:
        for owner in list(item.users_collection):
            owner.objects.unlink(item)
        self.collections[collection].objects.link(item)

    def __save_sources(self) -> None:
        base = self.collections["Base"].objects[0]
        export_material = self.__baked_export_material(base)
        base.data.materials.clear()
        base.data.materials.append(export_material)
        bpy.ops.wm.save_as_mainfile(filepath=str(self.output / "aizk-memory-cube.blend"))
        bpy.ops.object.select_all(action="DESELECT")
        for name in ("Base", "Node"):
            for item in self.collections[name].objects:
                item.select_set(True)
        bpy.ops.export_scene.gltf(
            filepath=str(self.output / "aizk-memory-cube.glb"),
            export_format="GLB",
            use_selection=True,
            export_apply=True,
        )

    def __baked_export_material(self, base: bpy.types.Object) -> bpy.types.Material:
        """Bake procedural folds into portable glTF color and normal textures."""
        scene = bpy.context.scene
        render_engine = scene.render.engine
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 16
        color = self.__bake_texture(base, "folds-color.png", "DIFFUSE")
        normal = self.__bake_texture(base, "folds-normal.png", "NORMAL")
        scene.render.engine = render_engine

        material = bpy.data.materials.new("Portable ceramic folds")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        color_texture = nodes.new("ShaderNodeTexImage")
        color_texture.image = color
        normal_texture = nodes.new("ShaderNodeTexImage")
        normal_texture.image = normal
        normal_texture.image.colorspace_settings.name = "Non-Color"
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.inputs["Strength"].default_value = 0.7
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Metallic"].default_value = 0.0
        shader.inputs["Roughness"].default_value = 0.4
        shader.inputs["Coat Weight"].default_value = 0.18
        shader.inputs["Coat Roughness"].default_value = 0.28
        output = nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(color_texture.outputs["Color"], shader.inputs["Base Color"])
        material.node_tree.links.new(normal_texture.outputs["Color"], normal_map.inputs["Color"])
        material.node_tree.links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])
        material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        return material

    def __bake_texture(
        self,
        base: bpy.types.Object,
        filename: str,
        bake_type: str,
    ) -> bpy.types.Image:
        """Bake one portable texture from the active procedural material."""
        image = bpy.data.images.new(filename, width=1024, height=1024, alpha=False)
        image.file_format = "PNG"
        image.filepath_raw = str(self.output / filename)
        material = base.data.materials[0]
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = image
        material.node_tree.nodes.active = image_node
        bpy.ops.object.select_all(action="DESELECT")
        base.select_set(True)
        bpy.context.view_layer.objects.active = base
        bpy.context.scene.render.bake.margin = 24
        if bake_type == "DIFFUSE":
            bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"}, use_clear=True)
        else:
            bpy.context.scene.render.bake.normal_space = "TANGENT"
            bpy.ops.object.bake(type="NORMAL", use_clear=True)
        image.save()
        material.node_tree.nodes.remove(image_node)
        return image

    def __render_material_studies(self) -> None:
        for name in ("ceramic", "resin", "metal"):
            self.__assign_collection_material("Base", self.materials[name])
            self.__assign_collection_material("Node", self.materials["coral"])
            self.__set_visibility(("Base", "Node", "Stage"))
            bpy.context.scene.render.film_transparent = False
            self.__render(self.render_output / f"{name}.png")

    def __render_icon_composer_layers(self) -> None:
        scene = bpy.context.scene
        scene.render.film_transparent = True
        scene.view_settings.look = "AgX - Medium High Contrast"
        layers = (
            ("01-base.png", "Base", "flat_base"),
            ("02-folds-periwinkle.png", "FoldsA", "flat_a"),
            ("03-folds-ivory.png", "FoldsB", "flat_b"),
            ("04-source-node.png", "Node", "flat_coral"),
        )
        for filename, collection, material in layers:
            self.__assign_collection_material(collection, self.materials[material])
            self.__set_visibility((collection,))
            self.__render(self.layer_output / filename)
        self.__assign_collection_material("Base", self.materials["flat_combined"])
        self.__assign_collection_material("Node", self.materials["flat_coral"])
        self.__set_visibility(("Base", "Node"))
        self.__render(self.layer_output / "preview.png")

    def __assign_collection_material(self, collection: str, material: bpy.types.Material) -> None:
        for item in self.collections[collection].objects:
            item.data.materials.clear()
            item.data.materials.append(material)

    def __set_visibility(self, visible: tuple[str, ...]) -> None:
        for name, collection in self.collections.items():
            collection.hide_render = name not in visible

    def __render(self, target: Path) -> None:
        bpy.context.scene.render.filepath = str(target)
        bpy.ops.render.render(write_still=True)


AizkBrandScene().render_all()
