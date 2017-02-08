from collections import namedtuple
import numpy as np
from scipy import optimize

from . import utilities, detect, vartype
from .signal_smooth import smooth

def _plot_line(ax, ranges, value, color):
    for (a,b) in ranges:
        ax.hlines(float(value), a, b, color, linestyles='-', zorder=3)
        if isinstance(value, vartype.vartype):
            ax.hlines([value.x - 3*value.dev, value.x + 3*value.dev], a, b,
                      color, linestyles='--', zorder=3)

class Feature:
    def __init__(self, obj):
        self._obj = obj

    def plot(self, figure):
        wave = self._obj.wave

        ax = figure.add_subplot(111)
        ax.plot(wave.x, wave.y, label='recording')
        ax.set_xlabel('time / s')
        ax.set_ylabel('membrane potential / V')
        return ax

class Baseline(Feature):
    """Find the baseline and injection steady states

    The range *before* `baseline_before` and *after* `baseline_after`
    is used for `baseline`.

    The range *between* `steady_after` and `steady_before` is used
    for `steady`.
    """
    requires = ('wave',
                'baseline_before', 'baseline_after',
                'steady_after', 'steady_before', 'steady_cutoff')
    provides = 'baseline', 'steady', 'response'

    @property
    @utilities.once
    def baseline(self):
        wave = self._obj.wave
        before = self._obj.baseline_before
        after = self._obj.baseline_after

        what = wave.y[(wave.x < before) | (wave.x > after)]
        cutoffa, cutoffb = np.percentile(what, (5, 95))
        cut = what[(what > cutoffa) & (what < cutoffb)]
        return vartype.array_mean(cut)

    @property
    @utilities.once
    def steady(self):
        wave = self._obj.wave
        after = self._obj.steady_after
        before = self._obj.steady_before
        cutoff = self._obj.steady_cutoff

        data = wave.y[(wave.x > after) & (wave.x < before)]
        cutoff = np.percentile(data, cutoff)
        cut = data[data < cutoff]
        return vartype.array_mean(cut)

    @property
    @utilities.once
    def response(self):
        return self.steady - self.baseline

    def plot(self, figure):
        wave = self._obj.wave
        before = self._obj.baseline_before
        after = self._obj.baseline_after
        time = wave.x[-1]

        ax = super().plot(figure)
        _plot_line(ax,
                   [(0, before), (after, time)],
                   self.baseline,
                   'k')
        _plot_line(ax,
                   [(after, before)],
                   self.steady,
                   'r')
        ax.annotate('response',
                    xy=(time/2, self.steady.x),
                    xytext=(time/2, self.baseline.x),
                    arrowprops=dict(facecolor='black'),
                    horizontalalignment='center', verticalalignment='bottom')

        ax.legend(loc='center right')
        figure.tight_layout()


def _find_spikes(wave, min_height=0.0):
    peaks = detect.detect_peaks(wave.y, P_low=0.75, P_high=0.20)
    return peaks[wave.y[peaks] > min_height]

class Spikes(Feature):
    """Find the position and height of spikes
    """
    requires = 'wave',
    provides = 'spike_i', 'spikes', 'spike_count'

    @property
    @utilities.once
    def spike_i(self):
        "Indices of spike maximums in the wave.x, wave.y arrays"
        return _find_spikes(self._obj.wave)

    @property
    @utilities.once
    def spikes(self):
        "An array with .x and .y components marking the spike maximums"
        return self._obj.wave[self.spike_i]

    @property
    def spike_count(self):
        "The number of spikes"
        return len(self.spike_i)

    def plot(self, figure):
        wave = self._obj.wave
        ax = super().plot(figure)

        ax.vlines(self.spikes.x, -0.06, self.spikes.y, 'r')
        ax.text(0.05, 0.5, '{} spikes'.format(self.spike_count),
                horizontalalignment='left',
                transform=ax.transAxes)
        figure.tight_layout()

        if self.spike_count > 0:
            ax2 = figure.add_axes([.7, .45, .25, .4])
            ax2.set_xlim(self.spikes.x[0] - 0.001, self.spikes.x[0] + 0.0015)
            ax2.plot(wave.x, wave.y, label='recording')
            ax2.vlines(self.spikes.x[:1], -0.06, self.spikes.y, 'r')
            ax2.tick_params(labelbottom='off', labelleft='off')
            ax2.set_title('first spike', fontsize='smaller')


def _find_falling_curve(wave, window=20, after=0.2, before=0.6):
    d = vartype.array_diff(wave)
    dd = smooth(d.y, window='hanning')[(d.x > after) & (d.x < before)]
    start = end = dd.argmin() + (d.x <= after).sum()
    while start > 0 and wave[start - 1].y > wave[start].y and wave[start].x > after:
        start -= 1
    sm = smooth(wave.y, window='hanning')
    smallest = sm[end]
    # find minimum
    while (end+window < wave.size and wave[end+window].x < before
           and sm[end:end + window].min() < smallest):
        smallest = sm[end]
        end += window // 2
    ccut = wave[start + 1 : end]
    return ccut

def simple_exp(x, amp, tau):
    return float(amp) * np.exp(-(x-x[0]) / float(tau))
def negative_exp(x, amp, tau):
    return float(amp) * (1-np.exp(-(x-x[0]) / float(tau)))

falling_param = namedtuple('falling_param', 'amp tau')
function_fit = namedtuple('function_fit', 'function params')

def _fit_falling_curve(ccut, baseline, steady):
    if ccut.size < 5:
        func = None
        params = falling_param(vartype(np.nan, np.nan),
                               vartype(np.nan, np.nan))
    else:
        init = (ccut.y.min()-baseline.x, ccut.x.ptp())
        func = negative_exp if (steady-baseline).negative else simple_exp
        popt, pcov = optimize.curve_fit(func, ccut.x, ccut.y-baseline.x, (-1,1))
        pcov = np.zeros((2,2)) + pcov
        params = falling_param(vartype.vartype(popt[0], pcov[0,0]**0.5),
                               vartype.vartype(popt[1], pcov[1,1]**0.5))
    return function_fit(func, params)


class FallingCurve(Feature):
    requires = ('wave',
                'steady_before', 'baseline_before',
                'falling_curve_window',
                 'baseline', 'steady')
    provides = 'falling_curve',

    @property
    @utilities.once
    def falling_curve(self):
        return _find_falling_curve(self._obj.wave,
                                   window=self._obj.falling_curve_window,
                                   before=self._obj.steady_before)

    @property
    @utilities.once
    def falling_curve_fit(self):
        return _fit_falling_curve(self.falling_curve, self._obj.baseline, self._obj.steady)

    def plot(self, figure):
        ax = super().plot(figure)

        ccut = self.falling_curve
        baseline = self._obj.baseline
        steady = self._obj.steady
        ax.plot(ccut.x, ccut.y, 'r', label='falling curve')
        ax.set_xlim(self._obj.baseline_before - 0.005, ccut.x.max() + .01)

        func, popt = self.falling_curve_fit
        label = 'fitted {}'.format(func.__name__)
        ax.plot(ccut.x, baseline.x + func(ccut.x, *popt), 'g--', label=label)

        ax.legend(loc='upper right')
        figure.tight_layout()


class Rectification(Feature):
    requires = ('baseline_before', 'steady_after', 'steady_before',
                'falling_curve', 'steady')
    provides = 'rectification',

    window_len = 11

    @property
    @utilities.once
    def rectification(self):
        ccut = self._obj.falling_curve
        steady = self._obj.steady

        if ccut.size < self.window_len + 1:
            return vartype.vartype(np.nan)
        pos = ccut.y.argmin()
        end = max(pos + self.window_len//2, ccut.size-1)
        bottom = vartype.array_mean(ccut[end-self.window_len : end+self.window_len+1].y)
        return steady - bottom

    def plot(self, figure):
        ax = super().plot(figure)

        ccut = self._obj.falling_curve
        after = self._obj.steady_after
        before = self._obj.steady_before
        steady = self._obj.steady

        ax.set_xlim(self._obj.baseline_before - 0.005, before)

        _plot_line(ax,
                   [(after, before)],
                   steady,
                   'r')
        right = (after + before) / 2
        bottom = steady.x - self.rectification.x
        _plot_line(ax,
                   [(after, right)],
                   bottom,
                   'g')
        ax.annotate('rectification',
                    xytext=(right, bottom),
                    xy=(right, self._obj.steady.x),
                    arrowprops=dict(facecolor='black'),
                    horizontalalignment='center', verticalalignment='top')

        ax.legend(loc='upper right')
        figure.tight_layout()
