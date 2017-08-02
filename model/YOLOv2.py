"""
Implementation of YOLOv2 Architecture on Keras

Few notes:
   - Its feature extractor is still DarkNet-19 (a VGG-ish CNN)
   - Its object detector structure is still the same. Instead of using regular CNN, however, I used MobileNet-like CNN
     in oder to reduce the number of parameters (from 57M --> 26.5M parameters )
"""
import tensorflow as tf
import keras.backend as K
from keras.layers.merge import concatenate
from keras.layers import Conv2D
from keras.layers import Lambda
from keras.models import Model
from keras.regularizers import l2
from model.net_builder import conv_block
from utils.augment_img import preprocess_img
from cfg import *


class YOLOv2(object):
    """
    YOLOv2 Meta-Architecture
    """
    def __init__(self, feature_extractor=None, num_anchors=N_ANCHORS, num_classes=N_CLASSES, fine_grain_layer='', dropout=None):
        """
        :param feature_extractor: A high-level CNN Classifier. One can plug and update an new feature extractor
                    e.g. :  Darknet19 (YOLOv2), MobileNet, ResNet-50
                    **NOTE** Update the SHRINK_FACTOR accordingly if you use other feature extractor different than DarkNet
        :param num_anchors: int
                    - number of anchors
        :param num_classes: int
                   - number of classes in training data
        """
        self.model = self._construct_yolov2(feature_extractor, num_anchors, num_classes, fine_grain_layer, dropout)

    def _construct_yolov2(self, feature_extractor, num_anchors, num_classes, fine_grain_layer, dropout=None):
        """
        Build YOLOv2 Model

        Input :  feature map from feature extractor
        Ouput :  prediction
        """
        fine_grained = feature_extractor.get_layer(name=fine_grain_layer[0]).output
        feature_map = feature_extractor.output
        x = conv_block(feature_map, 1024, (3, 3))
        x = conv_block(x, 1024, (3, 3))

        res_layer = conv_block(fine_grained, 64, (1, 1))
        reshaped = Lambda(space_to_depth_x2,
                          space_to_depth_x2_output_shape,
                          name='space_to_depth')(res_layer)

        x = concatenate([reshaped, x])
        x = conv_block(x, 1024, (3, 3))

        detector = Conv2D(filters=(num_anchors * (num_classes + 5)), name='yolov2', kernel_size=(1, 1))(x)

        YOLOv2 = Model(inputs=[feature_extractor.input], outputs=detector)
        return YOLOv2

    def predict(self, img, iou_threshold=0.5, score_threshold=0.4, mode = 0):
        """
        Perform a prediction with non-max suppression
        :param img:
        :param iou_threshold:
        :param score_threshold:
        :return:
        """
        input_img = preprocess_img(img)
        input_img = np.expand_dims(input_img, 0)

        # Making prediction
        prediction = self.model.predict(input_img)

        pred_shape = np.shape(prediction)
        prediction = np.reshape(prediction, [-1, pred_shape[1], pred_shape[2], N_ANCHORS, N_CLASSES+5])

        GRID_H, GRID_W = prediction.shape[1:3]

        # Create GRID-cell map
        cx = tf.cast((K.arange(0, stop=GRID_W)), dtype=tf.float32)
        cx = K.tile(cx, [GRID_H])
        cx = K.reshape(cx, [-1, GRID_H, GRID_W, 1])

        cy = K.cast((K.arange(0, stop=GRID_H)), dtype=tf.float32)
        cy = K.reshape(cy, [-1, 1])
        cy = K.tile(cy, [1, GRID_W])
        cy = K.reshape(cy, [-1])
        cy = K.reshape(cy, [-1, GRID_H, GRID_W, 1])

        c_xy = tf.stack([cx, cy], -1)
        c_xy = tf.to_float(c_xy)

        anchors_tensor = tf.to_float(K.reshape(K.variable(ANCHORS), [1, 1, 1, N_ANCHORS, 2]))
        netout_size = tf.to_float(K.reshape([GRID_W, GRID_H], [1, 1, 1, 1, 2]))

        box_xy          = K.sigmoid(prediction[..., :2])
        box_wh          = K.exp(prediction[..., 2:4])
        box_confidence  = K.sigmoid(prediction[..., 4:5])
        box_class_probs = prediction[..., 5:]

        # Shift center points to its grid cell accordingly (Ref: YOLO-9000 loss function)
        box_xy    = (box_xy + c_xy) / netout_size
        box_wh    = box_wh * anchors_tensor / netout_size
        box_mins  = box_xy - (box_wh / 2.)
        box_maxes = box_xy + (box_wh / 2.)

        # Y1, X1, Y2, X2
        boxes = K.concatenate([box_mins[..., 1:2], box_mins[..., 0:1], box_maxes[..., 1:2], box_maxes[..., 0:1]])

        # @TODO different level of soft-max
        if mode == 0:
            # Only get the first level of the soft max tree
            box_scores = box_confidence
        if mode == 1:
            id = HIER_TREE.tree_dict[-1].children[0].id
            box_scores = box_confidence * K.softmax(box_class_probs[..., id:id + len(HIER_TREE.tree_dict[-1].children)])
        if mode == 2:
            scores = []
            id = HIER_TREE.tree_dict[-1].children[0].id
            parent_scores = box_confidence * K.softmax(box_class_probs[..., id:id + len(HIER_TREE.tree_dict[-1].children)])
            for node in HIER_TREE.tree_dict[-1].children:
                id = node.children[0].id
                scores.append(parent_scores[..., node.id: node.id + 1] * K.softmax(
                    box_class_probs[..., id: id + len(node.children)]))
            box_scores = tf.concat(scores, axis=4)

        box_classes      = K.argmax(box_scores, -1)
        box_class_scores = K.max(box_scores, -1)
        prediction_mask  = (box_class_scores >= score_threshold)

        boxes = tf.boolean_mask(boxes, prediction_mask)
        scores = tf.boolean_mask(box_class_scores, prediction_mask)
        classes = tf.boolean_mask(box_classes, prediction_mask)

        # Scale boxes back to original image shape.
        height, width, _ = img.shape

        image_dims = tf.cast(K.stack([height, width, height, width]), tf.float32)
        image_dims = K.reshape(image_dims, [1, 4])
        boxes = boxes * image_dims

        nms_index = tf.image.non_max_suppression(boxes, scores, tf.Variable(10), iou_threshold=iou_threshold)
        boxes   = K.gather(boxes, nms_index)
        scores  = K.gather(scores, nms_index)
        classes = K.gather(classes, nms_index)

        init = tf.local_variables_initializer()
        K.get_session().run(init)
        boxes_prediction = boxes.eval()
        scores_prediction = scores.eval()
        classes_prediction = classes.eval()

        if mode == 0:
            classes_prediction -= 1
        if mode == 2:
            classes_prediction += len(HIER_TREE.tree_dict[-1].children)

        return boxes_prediction, scores_prediction, classes_prediction

    def loss(self):
        raise NotImplemented


def sigmoid(x):
    return 1. / (1.  + np.exp(-x))


def softmax(x):
    return np.exp(x) / np.sum(np.exp(x), axis=0)


def space_to_depth_x2(x):
    """Thin wrapper for Tensor flow space_to_depth with block_size=2."""
    # Import currently required to make Lambda work.
    import tensorflow as tf
    return tf.space_to_depth(x, block_size=2)


def space_to_depth_x2_output_shape(input_shape):
    """Determine space_to_depth output shape for block_size=2.
    Note: For Lambda with TensorFlow backend, output shape may not be needed.
    """
    return (input_shape[0], input_shape[1] // 2, input_shape[2] // 2, 4 * input_shape[3]) if input_shape[1] else \
        (input_shape[0], None, None, 4 * input_shape[3])
