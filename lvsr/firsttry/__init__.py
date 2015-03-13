from __future__ import print_function
import logging
import pprint
import math
import os
import functools
import cPickle
from collections import OrderedDict

import numpy
import theano
from numpy.testing import assert_allclose
from theano import tensor
from blocks.bricks import Tanh, MLP, Brick, application, Initializable
from blocks.bricks.recurrent import (
    SimpleRecurrent, GatedRecurrent, LSTM, Bidirectional)
from blocks.bricks.attention import SequenceContentAttention
from blocks.bricks.parallel import Fork
from blocks.bricks.sequence_generators import (
    SequenceGenerator, LinearReadout, SoftmaxEmitter, LookupFeedback)
from blocks.graph import ComputationGraph
from blocks.dump import load_parameter_values
from blocks.algorithms import (GradientDescent, Scale,
                               StepClipping, CompositeRule,
                               Momentum, RemoveNotFinite)
from blocks.initialization import Orthogonal, IsotropicGaussian, Constant
from blocks.monitoring import aggregation
from blocks.extensions import FinishAfter, Printing, Timing, ProgressBar
from blocks.extensions.saveload import Checkpoint, Dump
from blocks.extensions.monitoring import (
    TrainingDataMonitoring, DataStreamMonitoring)
from blocks.extensions.plot import Plot
from blocks.main_loop import MainLoop
from blocks.model import Model
from blocks.filter import VariableFilter
from blocks.utils import named_copy, dict_union
from blocks.search import BeamSearch
from blocks.select import Selector
from fuel.transformers import (
    SortMapping, Padding, ForceFloatX, Batch, Mapping, Unpack)
from fuel.schemes import SequentialScheme, ConstantScheme

from lvsr.datasets import TIMIT
from lvsr.preprocessing import log_spectrogram, Normalization
from lvsr.expressions import monotonicity_penalty, entropy, weights_std
from lvsr.error_rate import wer
from lvsr.attention import (
    ShiftPredictor, ShiftPredictor2, HybridAttention,
    SequenceContentAndCumSumAttention)

floatX = theano.config.floatX
logger = logging.getLogger(__name__)


def _length(example):
    return len(example[0])


def _gradient_norm_is_none(log):
    return math.isnan(log.current_row.total_gradient_norm)


def apply_preprocessing(preprocessing, example):
    recording, label = example
    return (numpy.asarray(preprocessing(recording)), label)


def switch_first_two_axes(batch):
    result = []
    for array in batch:
        if array.ndim == 2:
            result.append(array.transpose(1, 0))
        else:
            result.append(array.transpose(1, 0, 2))
    return tuple(result)


def build_stream(dataset, batch_size, sort_k_batches=None, normalization=None):
    if normalization:
        with open(normalization, "rb") as src:
            normalization = cPickle.load(src)

    stream = dataset.get_example_stream()
    if sort_k_batches:
        assert batch_size
        stream = Batch(stream,
                       iteration_scheme=ConstantScheme(
                           batch_size * sort_k_batches))
        stream = Mapping(stream, SortMapping(_length))
        stream = Unpack(stream)

    stream = Mapping(
        stream, functools.partial(apply_preprocessing,
                                       log_spectrogram))
    if normalization:
        stream = normalization.wrap_stream(stream)
    if not batch_size:
        return stream

    stream = Batch(stream, iteration_scheme=ConstantScheme(batch_size))
    stream = Padding(stream)
    stream = Mapping(
        stream, switch_first_two_axes)
    stream = ForceFloatX(stream)
    return stream


class Config(dict):

    def __getattr__(self, name):
        return self[name]


def default_config():
    return Config(
        net=Config(
            dim_dec=100, dim_bidir=100, dims_bottom=[100],
            enc_transition='SimpleRecurrent',
            dec_transition='SimpleRecurrent',
            attention_type='content',
            use_states_for_readout=False),
        initialization=[
            ('/recognizer', 'weights_init', 'IsotropicGaussian(0.1)'),
            ('/recognizer', 'biases_init', 'Constant(0.0)'),
            ('/recognizer', 'rec_weights_init', 'Orthogonal()')],
        data=Config(batch_size=10))


class PhonemeRecognizerBrick(Initializable):

    def __init__(self, num_features, num_phonemes,
                 dim_dec, dim_bidir, dims_bottom,
                 enc_transition, dec_transition,
                 use_states_for_readout,
                 attention_type,
                 shift_predictor_dims=None, max_left=None, max_right=None, **kwargs):
        super(PhonemeRecognizerBrick, self).__init__(**kwargs)
        self.rec_weights_init = None

        self.enc_transition = eval(enc_transition)
        self.dec_transition = eval(dec_transition)

        encoder = Bidirectional(self.enc_transition(
            dim=dim_bidir, activation=Tanh()))
        fork = Fork([name for name in encoder.prototype.apply.sequences
                    if name != 'mask'])
        fork.input_dim = dims_bottom[-1]
        fork.output_dims = [dim_bidir for name in fork.output_names]
        bottom = MLP([Tanh()] * len(dims_bottom), [num_features] + dims_bottom,
                     name="bottom")
        transition = self.dec_transition(
            dim=dim_dec, activation=Tanh(), name="transition")

        # Choose attention mechanism according to the configuration
        if attention_type == "content":
            attention = SequenceContentAttention(
                state_names=transition.apply.states,
                attended_dim=2 * dim_bidir, match_dim=dim_dec,
                name="cont_att")
        elif attention_type == "content_and_cumsum":
            attention = SequenceContentAndCumSumAttention(
                state_names=transition.apply.states,
                attended_dim=2 * dim_bidir, match_dim=dim_dec,
                name="cont_att")
        elif attention_type == "hybrid":
            predictor = MLP([Tanh(), None],
                            [None] + shift_predictor_dims + [None],
                            name="predictor")
            location_attention = ShiftPredictor(
                state_names=transition.apply.states,
                max_left=max_left, max_right=max_right,
                predictor=predictor,
                attended_dim=2 * dim_bidir,
                name="loc_att")
            attention = HybridAttention(
                state_names=transition.apply.states,
                attended_dim=2 * dim_bidir, match_dim=dim_dec,
                location_attention=location_attention,
                name="hybrid_att")
        elif attention_type == "hybrid2":
            predictor = MLP([Tanh(), None],
                            [None] + shift_predictor_dims + [None],
                            name="predictor")
            location_attention = ShiftPredictor2(
                state_names=transition.apply.states,
                predictor=predictor, attended_dim=2 * dim_bidir,
                name="loc_att")
            attention = HybridAttention(
                state_names=transition.apply.states,
                attended_dim=2 * dim_bidir, match_dim=dim_dec,
                location_attention=location_attention,
                name="hybrid_att")

        readout = LinearReadout(
            readout_dim=num_phonemes,
            source_names=(transition.apply.states if use_states_for_readout else [])
                + [attention.take_glimpses.outputs[0]],
            emitter=SoftmaxEmitter(name="emitter"),
            feedback_brick=LookupFeedback(num_phonemes, dim_dec),
            name="readout")
        generator = SequenceGenerator(
            readout=readout, transition=transition, attention=attention,
            name="generator")

        self.encoder = encoder
        self.fork = fork
        self.bottom = bottom
        self.generator = generator
        self.children = [encoder, fork, bottom, generator]

    def _push_initialization_config(self):
        super(PhonemeRecognizerBrick, self)._push_initialization_config()
        if self.rec_weights_init:
            self.encoder.weights_init = self.rec_weights_init
            self.generator.transition.transition.weights_init = self.rec_weights_init

    @application
    def cost(self, recordings, recordings_mask, labels, labels_mask):
        return self.generator.cost(
            labels, labels_mask,
            attended=self.encoder.apply(
                **dict_union(
                    self.fork.apply(self.bottom.apply(recordings),
                                    as_dict=True),
                    mask=recordings_mask)),
            attended_mask=recordings_mask)

    @application
    def generate(self, recordings):
        return self.generator.generate(
            n_steps=recordings.shape[0], batch_size=recordings.shape[1],
            attended=self.encoder.apply(
                **dict_union(self.fork.apply(self.bottom.apply(recordings),
                             as_dict=True))),
            attended_mask=tensor.ones_like(recordings[:, :, 0]))


class PhonemeRecognizer(object):

    def __init__(self, brick):
        self.brick = brick

        self.recordings = tensor.tensor3("recordings")
        self.recordings_mask = tensor.matrix("recordings_mask")
        self.labels = tensor.lmatrix("labels")
        self.labels_mask = tensor.matrix("labels_mask")
        self.single_recording = tensor.matrix("single_recording")
        self.single_transcription = tensor.lvector("single_transcription")

    def load_params(self, path):
        generated = self.get_generate_graph()
        Model(generated[1]).set_param_values(load_parameter_values(path))

    def get_generate_graph(self):
        return self.brick.generate(self.recordings)

    def get_cost_graph(self, batch=True):
        if batch:
            return self.brick.cost(
                       self.recordings, self.recordings_mask,
                       self.labels, self.labels_mask)
        recordings = self.single_recording[:, None, :]
        labels = self.single_transcription[:, None]
        return self.brick.cost(
            recordings, tensor.ones_like(recordings[:, :, 0]),
            labels, None)

    def analyze(self, recording, transcription):
        if not hasattr(self, "_analyze"):
            cost = self.get_cost_graph(batch=False)
            cg = ComputationGraph(cost)
            weights, = VariableFilter(
                bricks=[self.brick.generator], name="weights")(cg)
            self._analyze = theano.function(
                [self.single_recording, self.single_transcription],
                [cost[:, 0], weights[:, 0, :]])
        return self._analyze(recording, transcription)

    def init_beam_search(self, beam_size):
        self.beam_size = beam_size
        generated = self.get_generate_graph()
        samples, = VariableFilter(
            bricks=[self.generator], name="outputs")(
                ComputationGraph(generated[1]))
        self.beam_search = BeamSearch(beam_size, samples)
        self.beam_search.compile()

    def beam_search(self, recording):
        input_ = numpy.tile(recording, (self.beam_size, 1, 1)).transpose(1, 0, 2)
        outputs, search_costs = self.beam_search.search(
            {self.recognizer.recordings: input_}, 4, input_.shape[0] / 3,
            ignore_first_eol=True)
        return outputs, search_costs


def main(mode, save_path, num_batches, use_old, from_dump, config_path):
    # Experiment configuration
    config = default_config()
    if config_path:
        with open(config_path, 'rt') as config_file:
            changes = eval(config_file.read())
        def rec_update(conf, chg):
            for key in chg:
                if isinstance(conf.get(key), Config):
                    rec_update(conf[key], chg[key])
                elif isinstance(conf.get(key), list):
                    conf[key].extend(chg[key])
                else:
                    conf[key] = chg[key]
        rec_update(config, changes)
    logging.info("Config:\n" + pprint.pformat(config))

    if mode == "init_norm":
        stream = build_stream(TIMIT("train"), None)
        normalization = Normalization(stream, "recordings")
        with open(save_path, "wb") as dst:
            cPickle.dump(normalization, dst)

    elif mode == "show_data":
        stream = build_stream(TIMIT("train"), 10, **config.data)
        pprint.pprint(next(stream.get_epoch_iterator(as_dict=True)))

    elif mode == "train":
        root_path, extension = os.path.splitext(save_path)

        # Build the bricks
        assert not use_old
        recognizer = PhonemeRecognizerBrick(
            129, TIMIT.num_phonemes, name="recognizer", **config["net"])
        for brick_path, attribute, value in config['initialization']:
            brick, = Selector(recognizer).select(brick_path).bricks
            setattr(brick, attribute, eval(value))
            brick.push_initialization_config()
        recognizer.initialize()

        # Build the cost computation graph
        recordings = tensor.tensor3("recordings")
        recordings_mask = tensor.matrix("recordings_mask")
        labels = tensor.lmatrix("labels")
        labels_mask = tensor.matrix("labels_mask")
        batch_cost = recognizer.cost(
            recordings, recordings_mask, labels, labels_mask).sum()
        batch_size = named_copy(recordings.shape[1], "batch_size")
        cost = aggregation.mean(batch_cost,  batch_size)
        cost.name = "sequence_log_likelihood"
        logger.info("Cost graph is built")

        # Give an idea of what's going on
        model = Model(cost)
        params = model.get_params()
        logger.info("Parameters:\n" +
                    pprint.pformat(
                        [(key, value.get_value().shape) for key, value
                         in params.items()],
                        width=120))
        def show_init_scheme(cur):
            result = dict()
            for attr in ['weights_init', 'biases_init']:
                if hasattr(cur, attr):
                    result[attr] = getattr(cur, attr)
            for child in cur.children:
                result[child.name] = show_init_scheme(child)
            return result
        logger.info("Initialization:" +
                    pprint.pformat(show_init_scheme(recognizer)))

        cg = ComputationGraph(cost)
        r = recognizer
        # Fetch variables useful for debugging
        max_recording_length = named_copy(recordings.shape[0],
                                          "max_recording_length")
        max_num_phonemes = named_copy(labels.shape[0],
                                      "max_num_phonemes")
        cost_per_phoneme = named_copy(
            aggregation.mean(batch_cost, batch_size * max_num_phonemes),
            "phoneme_log_likelihood")
        (energies,) = VariableFilter(
            application=r.generator.readout.readout, name="output")(
                    cg.variables)
        min_energy = named_copy(energies.min(), "min_energy")
        max_energy = named_copy(energies.max(), "max_energy")
        (bottom_output,) = VariableFilter(
            application=r.bottom.apply, name="output")(cg)
        (attended,) = VariableFilter(
            application=r.generator.transition.apply, name="attended$")(cg)
        (weights,) = VariableFilter(
            application=r.generator.cost, name="weights")(cg)
        mean_attended = named_copy(abs(attended).mean(),
                                   "mean_attended")
        mean_bottom_output = named_copy(abs(bottom_output).mean(),
                                        "mean_bottom_output")
        weights_penalty = aggregation.mean(
            named_copy(monotonicity_penalty(weights, labels_mask),
                       "weights_penalty_per_recording"),
            batch_size)
        weights_entropy = aggregation.mean(
            named_copy(entropy(weights, labels_mask),
                       "weights_entropy_per_phoneme"),
            labels_mask.sum())
        mask_density = named_copy(labels_mask.mean(),
                                  "mask_density")

        # Define the training algorithm.
        algorithm = GradientDescent(
            cost=cost, params=cg.parameters,
            step_rule=CompositeRule([StepClipping(100.0),
                                     Scale(0.01),
                                     RemoveNotFinite(0.0)]))

        observables = [
            cost, cost_per_phoneme,
            min_energy, max_energy,
            mean_attended, mean_bottom_output,
            weights_penalty, weights_entropy,
            batch_size, max_recording_length, max_num_phonemes, mask_density,
            algorithm.total_step_norm, algorithm.total_gradient_norm]
        for name, param in params.items():
            observables.append(named_copy(
                param.norm(2), name + "_norm"))
            observables.append(named_copy(
                algorithm.gradients[param].norm(2), name + "_grad_norm"))

        every_batch = TrainingDataMonitoring(
            [algorithm.total_gradient_norm], after_every_batch=True)
        average = TrainingDataMonitoring(
            observables, prefix="average", every_n_batches=10)
        validation = DataStreamMonitoring(
            [cost, cost_per_phoneme],
            build_stream(TIMIT("valid"), **config["data"]), prefix="valid",
            before_first_epoch=True, on_resumption=True,
            after_every_epoch=True)
        main_loop = MainLoop(
            model=model,
            data_stream=build_stream(
                TIMIT("train"), **config["data"]),
            algorithm=algorithm,
            extensions=([
                Timing(),
                every_batch, average, validation,
                FinishAfter(after_n_batches=num_batches)
                .add_condition("after_batch", _gradient_norm_is_none),
                Plot(os.path.basename(save_path),
                     [[average.record_name(cost),
                       validation.record_name(cost)],
                      [average.record_name(cost_per_phoneme)],
                      [average.record_name(algorithm.total_gradient_norm)],
                      [average.record_name(weights_entropy)]],
                     every_n_batches=10),
                Checkpoint(save_path,
                           before_first_epoch=True, after_every_epoch=True,
                           save_separately=["model"]),
                Dump(os.path.splitext(save_path)[0], after_every_epoch=True),
                ProgressBar(),
                Printing(every_n_batches=1)]))
        main_loop.run()
    elif mode == "search":
        recognizer_brick, = cPickle.load(open(save_path)).get_top_bricks()
        recognizer = PhonemeRecognizer(recognizer_brick)
        recognizer.init_beam_search(10)

        timit = TIMIT("valid")
        conf = config["data"]
        conf['batch_size'] = conf['sort_k_batches'] = None
        stream = build_stream(timit, **conf)
        stream = ForceFloatX(stream)
        it = stream.get_epoch_iterator()

        weights = tensor.matrix('weights')
        weight_statistics = theano.function(
            [weights],
            [weights_std(weights.dimshuffle(0, 'x', 1)),
             monotonicity_penalty(weights.dimshuffle(0, 'x', 1))])

        error_sum = 0
        for number, data in enumerate(it):
            print("Utterance", number)

            outputs, search_costs = recognizer.beam_search()
            recognized = timit.decode(outputs[0])
            groundtruth = timit.decode(data[1])
            costs_recognized, weights_recognized = (
                recognizer.analyze(data[0], outputs[0]))
            costs_groundtruth, weights_groundtruth = (
                recognizer.analyze(data[0], data[1]))
            weight_std_recognized, mono_penalty_recognized = weight_statistics(
                weights_recognized)
            weight_std_groundtruth, mono_penalty_groundtruth = weight_statistics(
                weights_groundtruth)
            error = min(1, wer(groundtruth, recognized))
            error_sum += error

            print("Beam search cost:", search_costs[0])
            print(recognized)
            print("Recognized cost:", costs_recognized.sum())
            print("Recognized weight std:", weight_std_recognized)
            print("Recognized monotonicity penalty:", mono_penalty_recognized)
            print(groundtruth)
            print("Groundtruth cost:", costs_groundtruth.sum())
            print("Groundtruth weight std:", weight_std_groundtruth)
            print("Groundtruth monotonicity penalty:", mono_penalty_groundtruth)
            print("PER:", error)
            print("Average PER:", error_sum / (number + 1))

            assert_allclose(search_costs[0], costs_recognized.sum(), rtol=1e-5)
