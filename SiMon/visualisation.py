import os
import matplotlib.pyplot as plt
import numpy as np
import math
from matplotlib.colors import ListedColormap, BoundaryNorm
from matplotlib.collections import LineCollection
from matplotlib import cm
from SiMon.module_common import SimulationTask
from matplotlib.ticker import MaxNLocator
import time

def progress_graph(sim_list):
    """
    Creates a graph showing the progress of the simulations

    :param num_sim: number of simulations

    :return:
    """

    num_sim = len(sim_list)
    status = []
    progresses = []

    # Checks if num_sim has a square
    if int(math.sqrt(num_sim) + 0.5) ** 2 == num_sim:
        number = int(math.sqrt(num_sim))
        y_num = num_sim // number

    # If not square, find divisible number to get rectangle
    else:
        number = int(math.sqrt(num_sim))
        while num_sim % number != 0:
            number = number - 1
        y_num = num_sim // number                               # Y-axis limit

        # If prime number
        if number == 1:
            number = int(math.sqrt(num_sim)) + 1                # Make sure graph fits all num_sim
            y_num = number
            # 'Removes' extra white line if graph is too big
            if (y_num * number) > num_sim and ((y_num - 1) * number) >= num_sim:
                y_num = y_num - 1

    x_sim = num_sim % number
    y_sim = num_sim // number

    plt.figure(1, figsize=(12, 12))
    ax = plt.gca()                                          # get the axis
    ax.set_ylim(ax.get_ylim()[::-1])                        # invert the axis
    ax.xaxis.tick_top()                                     # and move the X-Axis
    ax.yaxis.set_ticks(np.arange(-0.5, y_num))              # set y-ticks
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))   # set to integers
    ax.yaxis.tick_left()                                    # remove right y-Ticks

    symbols = ['s', '>', 'x',  '^', 'o',  '*']
    labels = ['STOP', 'RUN', 'ERROR', 'STALL', 'WARN', 'DONE']
    for i, symbol in enumerate(symbols):
        print(i, symbol),
        plt.scatter(
            x_sim[status == i],
            y_sim[status == i],
            marker=symbol,
            s=500,
            c=progresses[status == i],
            cmap=cm.RdYlBu,
            label=labels[i])

    for i in range(num_sim.shape[0]):
        plt.annotate(
            text=str(i),
            xy=(x_sim[i], y_sim[i]),
            color='black',
            weight='bold',
            size=15
        )

    plt.legend(
        bbox_to_anchor=(0., -.15, 1., .102),
        loc='lower center',
        ncol=4,
        mode="expand",
        borderaxespad=0.,
        borderpad=2,
        labelspacing=3
    )

    plt.colorbar()

    # Save file with a new name
    if os.path.exists('progress.pdf'):
        plt.savefig('progress_{}.pdf'.format(int(time.time())))
    else:
        plt.savefig('progress.pdf')