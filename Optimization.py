import argparse
import os
import sys
import models
import dataset
import numpy as np
import tensorflow as tf
from multiprocessing import Queue


def load_model(name, input_node):
    """ Creates and returns an instance of the model given its class name.
    The created model has a single placeholder node for feeding images.
    """
    # Find the model class from its name
    all_models = models.get_models()
    net_class = [model for model in all_models if model.__name__ == name][0]

    # Construct and return the model
    return net_class({'data': input_node})


def calc_gradients(
        model_name,
        image_producer,
        output_file_dir,
        max_iter,
        save_freq,
        learning_rate=1.0,
        targets=None,
        weight_loss2=1,
        data_spec=None,
        batch_size=1,
        noise_file=None):
    """Compute the gradients for the given network and images."""

    spec = models.get_data_spec(model_name)

    modifier = tf.Variable(
        np.zeros(
            (batch_size,
             spec.crop_size,
             spec.crop_size,
             spec.channels),
            dtype=np.float32))
    input_image = tf.placeholder(
        tf.float32, (None, spec.crop_size, spec.crop_size, spec.channels))
    input_label = tf.placeholder(tf.int32, (None))

    true_image = tf.minimum(tf.maximum(modifier +
                                       input_image, -
                                       spec.mean +
                                       spec.rescale[0]), -
                            spec.mean +
                            spec.rescale[1])
    diff = true_image - input_image
    loss2 = tf.sqrt(tf.reduce_mean(tf.square(true_image - input_image)))

    sesh = tf.Session()

    probs, variable_set = models.get_model(sesh, true_image, model_name)

    weight_loss1 = 1
    true_label_prob = tf.reduce_mean(
        tf.reduce_sum(
            probs *
            tf.one_hot(
                input_label,
                1000),
            [1]))
    if targets is None:
        loss1 = -tf.log(1 - true_label_prob + 1e-6)
    else:
        loss1 = -tf.log(true_label_prob + 1e-6)
    loss = weight_loss1 * loss1  # + weight_loss2 * loss2
    optimizer = tf.train.AdamOptimizer(learning_rate)
    train = optimizer.minimize(loss, var_list=[modifier])

    noise = None
    # Load noise file
    if noise_file is not None:
        noise = np.load(noise_file) / 255.0 * \
            (spec.rescale[1] - spec.rescale[0])

    # The number of images processed
    # The total number of images
    total = len(image_producer)
    save_times = (max_iter - 1) / save_freq + 1
    gradient_record = np.zeros(
        shape=(
            save_times,
            total,
            spec.crop_size,
            spec.crop_size,
            spec.channels),
        dtype=float)
    rec_iters = []
    rec_names = []
    rec_dist = []

    # initiallize all uninitialized varibales
    init_varibale_list = set(tf.all_variables()) - variable_set
    sesh.run(tf.initialize_variables(init_varibale_list))

    coordinator = tf.train.Coordinator()
    # Start the image processing workers
    threads = image_producer.start(session=sesh, coordinator=coordinator)
    image_producer.startover(sesh)

    tot_image = 0

    # Interactive with mini-batches
    for (indices, labels, names, images) in image_producer.batches(sesh):
        sesh.run(tf.initialize_variables(init_varibale_list))
        if targets is not None:
            labels = [targets[e] for e in names]
        if noise is not None:
            for i in range(len(indices)):
                images[i] += noise[indices[i]]
        feed_dict = {input_image: images, input_label: labels}
        var_loss, true_prob, var_loss1, var_loss2 = sesh.run(
            (loss, true_label_prob, loss1, loss2), feed_dict=feed_dict)
        tot_image += 1
        print 'Start!'
        min_loss = var_loss
        last_min = -1

        # record numer of iteration
        tot_iter = 0
        for cur_iter in range(max_iter):
            tot_iter += 1

            sesh.run(train, feed_dict=feed_dict)
            var_loss, true_prob, var_loss1, var_loss2 = sesh.run(
                (loss, true_label_prob, loss1, loss2), feed_dict=feed_dict)

            break_condition = False
            if var_loss < min_loss * 0.99:
                min_loss = var_loss
                last_min = cur_iter

            if (cur_iter + 1) % save_freq == 0:
                noise_diff = sesh.run(modifier)
                for i in range(len(indices)):
                    gradient_record[(cur_iter + 1) / save_freq -
                                    1][indices[i]] = noise_diff[i]

            if cur_iter + 1 == max_iter or break_condition:
                var_diff, var_probs = sesh.run(
                    (modifier, probs), feed_dict=feed_dict)
                var_diff = np.sqrt(np.mean(np.square(
                    var_diff), (1, 2, 3))) / (spec.rescale[1] - spec.rescale[0]) * 255.0
                correct_top_1 = 0
                for i in range(len(indices)):
                    top1 = var_probs[i].argmax()
                    if labels[i] == top1:
                        correct_top_1 += 1
                    rec_iters.append(tot_iter)
                    rec_names.append(names[i])
                    rec_dist.append(var_diff[i])
                break

    # Close queue
    image_producer.close_queue(session=sesh)
    # Stop the worker threads
    coordinator.request_stop()
    coordinator.join(threads, stop_grace_period_secs=2)

    if output_file_dir is not None:
        if not os.path.exists(output_file_dir):
            os.makedirs(output_file_dir)
        for i in range(save_times):
            np.save(os.path.join(output_file_dir, model_name + '-' +
                                 str((i + 1) * save_freq)), gradient_record[i])
    with open(os.path.join(output_file_dir, model_name + '_log.txt'), 'w') as f:
        f.write('Average numer of iterations: %.2f\n' % np.mean(rec_iters))
        f.write('Average L2 distance %.2f\n' % np.mean(rec_dist))
        for i in range(len(rec_names)):
            f.write('%s %d %.2f\n' % (rec_names[i], rec_iters[i], rec_dist[i]))


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(
        description='Evaluate model on some dataset.')
    parser.add_argument(
        '-i',
        '--input_dir',
        type=str,
        required=True,
        help='Directory of dataset.')
    parser.add_argument(
        '-o',
        '--output_dir',
        type=str,
        default=None,
        help='Directory of output noise file.')
    parser.add_argument(
        '--model',
        type=str,
        required=True,
        choices=[
            'Inception2',
            'Inception',
            'ResNet50',
            'ResNet101',
            'ResNet152',
            'VGG16',
            'AlexNet',
            'GoogleNet'],
        help='Models to be evaluated.')
    parser.add_argument(
        '--num_images',
        type=int,
        default=sys.maxsize,
        help='Max number of images to be evaluated.')
    parser.add_argument('--file_list', type=str, default=None,
                        help='Evaluate a specific list of file in dataset.')
    parser.add_argument('--noise_file', type=str, default=None,
                        help='Directory of the noise file.')
    parser.add_argument(
        '--num_iter',
        type=int,
        default=1000,
        help='Number of iterations to generate attack.')
    parser.add_argument(
        '--save_freq',
        type=int,
        default=10,
        help='Save .npy file when each save_freq iterations.')
    parser.add_argument(
        '--learning_rate',
        type=float,
        default=0.001 * 255,
        help='Learning rate of each iteration.')
    parser.add_argument('--target', type=str, default=None,
                        help='Target list of dataset.')
    parser.add_argument(
        '--weight_loss2',
        type=float,
        default=0.0,
        help='Weight of distance penalty.')
    parser.add_argument(
        '--not_crop',
        dest='use_crop',
        action='store_false',
        help='Not use crop in image producer.')

    parser.set_defaults(use_crop=True)
    args = parser.parse_args()

    assert args.num_iter % args.save_freq == 0

    data_spec = models.get_data_spec(model_name=args.model)
    args.learning_rate = args.learning_rate / 255.0 * \
        (data_spec.rescale[1] - data_spec.rescale[0])
    image_producer = dataset.ImageNetProducer(file_list=args.file_list,
                                              data_path=args.input_dir,
                                              num_images=args.num_images,
                                              data_spec=data_spec,
                                              need_rescale=args.use_crop,
                                              batch_size=1)

    targets = None
    if args.target is not None:
        targets = {}
        with open(args.target, 'r') as f:
            for line in f:
                key, value = line.strip().split()
                targets[key] = int(value)

    calc_gradients(
        args.model,
        image_producer,
        args.output_dir,
        args.num_iter,
        args.save_freq,
        args.learning_rate,
        targets,
        args.weight_loss2,
        data_spec,
        1,
        args.noise_file)


if __name__ == '__main__':
    main()