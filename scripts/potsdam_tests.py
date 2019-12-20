import os
from os.path import join

import rastervision as rv

import tempfile
import os

import rasterio
from shapely.strtree import STRtree
from shapely.geometry import shape

from rastervision.core import Box
from rastervision.data import RasterioCRSTransformer, GeoJSONVectorSource
from rastervision.utils.files import (file_exists, get_local_path, upload_or_copy, make_dir)
from rastervision.filesystem import S3FileSystem


def str_to_bool(x):
    if type(x) == str:
        if x.lower() == 'true':
            return True
        elif x.lower() == 'false':
            return False
        else:
            raise ValueError('{} is expected to be true or false'.format(x))
    return x


def save_image_crop(image_uri, crop_uri, label_uri=None, size=600,
                    min_features=10):
    """Save a crop of an image to use for testing.
    If label_uri is set, the crop needs to cover >= min_features.
    Args:
        image_uri: URI of original image
        crop_uri: URI of cropped image to save
        label_uri: optional URI of GeoJSON file
        size: height and width of crop
    Raises:
        ValueError if cannot find a crop satisfying min_features constraint.
    """
    if not file_exists(crop_uri):
        print('Saving test crop to {}...'.format(crop_uri))
        old_environ = os.environ.copy()
        try:
            request_payer = S3FileSystem.get_request_payer()
            if request_payer == 'requester':
                os.environ['AWS_REQUEST_PAYER'] = request_payer
            im_dataset = rasterio.open(image_uri)
            h, w = im_dataset.height, im_dataset.width

            extent = Box(0, 0, h, w)
            windows = extent.get_windows(size, size)
            if label_uri is not None:
                crs_transformer = RasterioCRSTransformer.from_dataset(im_dataset)
                vs = GeoJSONVectorSource(label_uri, crs_transformer)
                geojson = vs.get_geojson()
                geoms = []
                for f in geojson['features']:
                    g = shape(f['geometry'])
                    geoms.append(g)
                tree = STRtree(geoms)

            for w in windows:
                use_window = True
                if label_uri is not None:
                    w_polys = tree.query(w.to_shapely())
                    use_window = len(w_polys) >= min_features

                if use_window:
                    w = w.rasterio_format()
                    im = im_dataset.read(window=w)

                    with tempfile.TemporaryDirectory() as tmp_dir:
                        crop_path = get_local_path(crop_uri, tmp_dir)
                        make_dir(crop_path, use_dirname=True)

                        meta = im_dataset.meta
                        meta['width'], meta['height'] = size, size
                        meta['transform'] = rasterio.windows.transform(
                            w, im_dataset.transform)

                        with rasterio.open(crop_path, 'w', **meta) as dst:
                            dst.colorinterp = im_dataset.colorinterp
                            dst.write(im)

                        upload_or_copy(crop_path, crop_uri)
                    break

            if not use_window:
                raise ValueError('Could not find a good crop.')
        finally:
            os.environ.clear()
            os.environ.update(old_environ)


class PotsdamSemanticSegmentation(rv.ExperimentSet):
    # def exp_main(self, raw_uri, processed_uri, root_uri, test=False):
    def exp_main(self):
        """Run an experiment on the ISPRS Potsdam dataset.
        Uses Pytorch Deeplab backend with Resnet50.
        Args:
            raw_uri: (str) directory of raw data
            root_uri: (str) root directory for experiment output
            test: (bool) if True, run a very small experiment as a test and generate
                debug output
        """
        raw_uri = "/home/maturgeo/rastervision_tests/potsdam_data"
        processed_uri = "/home/maturgeo/rastervision_tests/scrap"
        root_uri = "/home/maturgeo/rastervision_tests/output"

        test = str_to_bool(False)
        exp_id = 'potsdam-seg'
        train_ids = ['2-10', '2-11', '2-13', '2-14',
                     '3-10', '3-11', '3-13', '3-14',
                     '4-10', '4-11', '4-13', '4-14',
                     '5-10', '5-11', '5-13', '5-14', '5-15',
                     '6-10', '6-11', '6-13', '6-14', '6-15', '6-7', '6-8', '6-9',
                     '7-10', '7-11', '7-13', '7-7', '7-8', '7-9']

        val_ids = ['2-12', '3-12', '4-12', '5-12', '6-12', '7-12']
        channel_order = [0, 1, 2]
        debug = False

        if test:
            debug = True
            train_ids = train_ids[0:1]
            val_ids = val_ids[0:1]
            exp_id += '-test'

        classes = {
            'Car': (1, '#ffff00'),
            'Building': (2, '#0000ff'),
            'Low Vegetation': (3, '#00ffff'),
            'Tree': (4, '#00ff00'),
            'Impervious': (5, "#ffffff"),
            'Clutter': (6, "#ff0000")
        }

        task = rv.TaskConfig.builder(rv.SEMANTIC_SEGMENTATION) \
                            .with_chip_size(300) \
                            .with_classes(classes) \
                            .with_chip_options(window_method='sliding',
                                               stride=300, debug_chip_probability=1.0) \
                            .build()

        batch_size = 16
        num_epochs = 100
        if test:
            batch_size = 2
            num_epochs = 1

        backend = rv.BackendConfig.builder(rv.PYTORCH_SEMANTIC_SEGMENTATION) \
            .with_task(task) \
            .with_train_options(
                lr=1e-3,
                batch_size=batch_size,
                num_epochs=num_epochs,
                debug=debug) \
            .build()

        def make_scene(id):
            id = id.replace('-', '_')
            raster_uri = '{}/2_Ortho_RGB/top_potsdam_{}_RGB.tif'.format(
                raw_uri, id)

            label_uri = '{}/5_Labels_all/top_potsdam_{}_label.tif'.format(
                raw_uri, id)

            if test:
                crop_uri = join(
                    processed_uri, 'crops', os.path.basename(raster_uri))
                save_image_crop(raster_uri, crop_uri, size=600)
                raster_uri = crop_uri

            # Using with_rgb_class_map because label TIFFs have classes encoded as RGB colors.
            label_source = rv.LabelSourceConfig.builder(rv.SEMANTIC_SEGMENTATION) \
                .with_rgb_class_map(task.class_map) \
                .with_raster_source(label_uri) \
                .build()

            # URI will be injected by scene config.
            # Using with_rgb(True) because we want prediction TIFFs to be in RGB format.
            label_store = rv.LabelStoreConfig.builder(rv.SEMANTIC_SEGMENTATION_RASTER) \
                .with_rgb(True) \
                .build()

            scene = rv.SceneConfig.builder() \
                                  .with_task(task) \
                                  .with_id(id) \
                                  .with_raster_source(raster_uri,
                                                      channel_order=channel_order) \
                                  .with_label_source(label_source) \
                                  .with_label_store(label_store) \
                                  .build()

            return scene

        train_scenes = [make_scene(id) for id in train_ids]
        val_scenes = [make_scene(id) for id in val_ids]

        dataset = rv.DatasetConfig.builder() \
                                  .with_train_scenes(train_scenes) \
                                  .with_validation_scenes(val_scenes) \
                                  .build()

        experiment = rv.ExperimentConfig.builder() \
                                        .with_id(exp_id) \
                                        .with_task(task) \
                                        .with_backend(backend) \
                                        .with_dataset(dataset) \
                                        .with_root_uri(root_uri) \
                                        .build()

        return experiment


if __name__ == '__main__':
    rv.main()
