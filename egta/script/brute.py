import argparse
import json
import logging

import numpy as np
from gameanalysis import nash
from gameanalysis import regret
from gameanalysis import restrict

from egta import schedgame
from egta.script import utils


def add_parser(subparsers):
    parser = subparsers.add_parser(
        'brute', help="""Compute equilibria by sampling all profiles""",
        description="""Samples profiles from the entire game, and then runs
        standard equilibrium finding. For games with a large number of players,
        a reduction should be specified. A list of is returned where each
        element has an "equilibrium" and the corresponding "regret" in the full
        game.""")
    parser.add_argument('scheduler', type=utils.scheduler, help="""FIXME""")
    parser.add_argument(
        '--regret-thresh', metavar='<reg>', type=float, default=1e-3,
        help="""Regret threshold for a mixture to be considered an equilibrium.
        (default: %(default)g)""")
    parser.add_argument(
        '--dist-thresh', metavar='<norm>', type=float, default=0.1,
        help="""Norm threshold for two mixtures to be considered distinct.
        (default: %(default)g)""")
    parser.add_argument(
        '--supp-thresh', metavar='<min-prob>', type=float, default=1e-4,
        help="""Minimum probability for a strategy to be considered in support.
        (default: %(default)g)""")
    parser.add_argument(
        '--restrict', '-r', metavar='<restriction-file>',
        type=argparse.FileType('r'), help="""Specify an optional restricted
        game to sample instead of the whole game. Only deviations from the
        restricted strategy set will be scheduled.""")
    utils.add_reductions(parser)

    parser.run = run
    return parser


async def run(args):
    scheduler = args.scheduler
    game = scheduler.game()
    red, red_players = utils.parse_reduction(game, args)

    rest = (np.ones(game.num_strats, bool) if args.restrict is None
            else game.restriction_from_json(json.load(args.restrict)))

    async with scheduler:
        data = await schedgame.schedgame(
            scheduler, red, red_players).get_deviation_game(rest)

    # now find equilibria
    eqa = game.trim_mixture_support(restrict.translate(nash.mixed_nash(
        data.restrict(rest), regret_thresh=args.regret_thresh,
        dist_thresh=args.dist_thresh), rest), thresh=args.supp_thresh)
    reg_info = []
    for eqm in eqa:
        gains = regret.mixture_deviation_gains(data, eqm)
        bri = np.argmax(gains)
        reg_info.append((gains[bri],) + game.role_strat_names[bri])

    logging.error(
        "brute sampling finished finding %d equilibria:\n%s",
        eqa.shape[0], '\n'.join(
            '{:d}) {} with regret {:g} to {} {}'.format(
                i, game.mixture_to_repr(eqm), reg, role, strat)
            for i, (eqm, (reg, role, strat))
            in enumerate(zip(eqa, reg_info), 1)))

    json.dump([{'equilibrium': game.mixture_to_json(eqm),
                'regret': reg,
                'best_response': {'role': role, 'strat': strat}}
               for eqm, (reg, role, strat)
               in zip(eqa, reg_info)], args.output)
    args.output.write('\n')
