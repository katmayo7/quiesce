"""Python script for quiessing a game"""
import argparse
import heapq
import json
import logging
import smtplib
import sys
import time
import traceback
import warnings
from logging import handlers
from os import path

import numpy as np
from numpy import linalg

from gameanalysis import gameio
from gameanalysis import nash
from gameanalysis import reduction
from gameanalysis import regret
from gameanalysis import subgame

from egtaonline import api
from egtaonline import profsched
from egtaonline import utils


# FIXME Remove all instances of utils.format_json
# FIXME Add option to explore one role at a time
# FIXME Save state and resume on error

# Game load failure is a user warning, but we don't want to process it
warnings.simplefilter('error', UserWarning)


_def_auth = path.join(path.dirname(path.dirname(__file__)), 'auth_token.txt')

_parser = argparse.ArgumentParser(prog='quiesce', description="""Quiesce a
                                  generic scheduler on EGTA Online.""")
_parser.add_argument('game', metavar='<game-id>', type=int, help="""The id of
                     the game to pull data from / to quiesce""")
_parser.add_argument('-p', '--max-profiles', metavar='<max-num-profiles>',
                     type=int, default=500, help="""Maximum number of profiles
                     to ever have scheduled at a time. (default:
                     %(default)s)""")
_parser.add_argument('-t', '--sleep-time', metavar='<sleep-time>', type=int,
                     default=300, help="""Time to wait in seconds between
                     checking EGTA Online for job completion. (default:
                     %(default)s)""")
_parser.add_argument('-m', '--max-subgame-size', metavar='<max-subgame-size>',
                     type=int, default=3, help="""Maximum subgame size to
                     require exploration. (default: %(default)d)""")
_parser.add_argument('--num-equilibria', '-n', metavar='<num-equilibria>',
                     default=1, type=int, help="""Necessary number of
                     equilibria to find to consider quiesced. This is useful if
                     you want to include a strategy that results in a trivial
                     equilibrium e.g. a no-op. (default: %(default)d)""")
# TODO add json input
_parser.add_argument('--dpr', nargs='+', metavar='<role-or-count>',
                     help="""If specified, does a dpr reduction with role
                     strategy counts.  e.g.  --dpr role1 1 role2 2 ...""")
_parser.add_argument('-v', '--verbose', action='count', default=0,
                     help="""Verbosity level. Two for confirmed equilibria,
                     three for major scheduling actions, four for minor
                     scheduling actions (i.e. every profile), five for all
                     requests (these won't go to email). Logging is output to
                     standard error""")
_parser.add_argument('-e', '--email_verbosity', action='count', default=0,
                     help="""Verbosity level for email. Two for confirmed
                     equilibria, three for everything""")
_parser.add_argument('-r', '--recipient', metavar='<email-address>',
                     action='append', default=[], help="""Specify an email
                     address to receive email logs at. Can specify multiple
                     email addresses.""")
_parser.add_argument('--regret-threshold', metavar='<regret-threshold>',
                     default=1e-3, type=float, help="""Regret tolerance to
                     consider an equilibrium found. (default: %(default)s)""")

_parser_auth = _parser.add_mutually_exclusive_group()
_parser_auth.add_argument('--auth-string', '-a', metavar='<auth-string>',
                          help="""The string authorization token to connect to
                          egta online.""")
_parser_auth.add_argument('--auth-file', '-f', metavar='<auth-file>',
                          default=_def_auth, help="""Filename that just
                          contains the string of the auth token. (default:
                          %(default)s)""")

_sched_group = _parser.add_argument_group('Scheduler parameters',
                                          description="""Parameters for the
                                          scheduler.""")
_sched_group.add_argument('-y', '--memory', metavar='<process-memory>',
                          type=int, default=4096, help="""The process memory to
                          schedule jobs with in MB.  (default: %(default)s)""")
_sched_group.add_argument('-o', '--observation-time',
                          metavar='<observation-time>', type=int, default=600,
                          help="""The time to allow for each observation in
                          seconds. (default: %(default)s)""")
_sched_group.add_argument('--observation-increment', '-b',
                          metavar='<observation-increment>', type=int,
                          default=10, help="""The number of observations to run
                          per simulation. (default: %(default)s)""")
_sched_group.add_argument('--nodes', metavar='<nodes>', type=int, default=1,
                          help="""Number of nodes to run the simulation on.
                          (default: %(default)s)""")


def quiesce(sim, game, serial, base_name, configuration={}, dpr=None,
            log=logging, max_profiles=500, max_subgame_size=3,
            sleep_time=300, required_equilibria=1, regret_thresh=1e-3,
            reschedule_limit=10, process_memory=4096, observation_time=600,
            observation_increment=1, nodes=1):
    """Quiesce a game"""

    # Create scheduler
    sched = sim.create_generic_scheduler(
        name='{base}_generic_quiesce_{random}'.format(
            base=base_name, random=utils.random_string(6)),
        active=1,
        process_memory=process_memory,
        size=game.num_players.sum(),
        time_per_observation=observation_time,
        observations_per_simulation=observation_increment,
        nodes=nodes,
        default_observation_requirement=observation_increment,
        configuration=configuration)

    try:
        # Add roles and counts to scheduler
        for role, count in zip(serial.role_names, game.num_players):
            sched.add_role(role, count)

        log.info('Created scheduler %d '
                 '(http://egtaonline.eecs.umich.edu/generic_schedulers/%d)',
                 sched.id, sched.id)

        # Set up reduction
        if dpr is None:
            red = reduction.Identity()
        else:
            red = reduction.DeviationPreserving(game.num_strategies,
                                                game.num_players, dpr)

        # Set up main scheduler abstraction
        qsched = profsched.QuiesceScheduler(game, serial, red, sched,
                                            max_profiles, log)

        confirmed_equilibria = []  # Confirmed equilibra
        explored_subgames = []  # Already explored subgames
        explored_mixtures = []  # Already explored mixtures
        backup = []  # Extra subgames to explore
        subgames = []  # Subgames that are scheduling
        deviations = []  # Deviations that are scheduling

        # Define useful functions
        def add_subgame(subm):
            """Adds a subgame to the scheduler"""
            if not any(np.all(subm <= s) for s in explored_subgames):  # Unexplored
                explored_subgames[:] = [s for s in explored_subgames
                                        if np.any(s > subm)]
                explored_subgames.append(subm)
                log.debug('Exploring subgame:\n%s\n', json.dumps(
                    {r: list(s) for r, s in serial.to_prof_json(subm).items()},
                    indent=2))
                subgames.append(
                    qsched.schedule_subgame(subm, observation_increment))
            else:  # Subgame already explored
                log.debug('Subgame already explored:\n%s\n', json.dumps(
                    {r: list(s) for r, s in serial.to_prof_json(subm).items()},
                    indent=2))

        def add_equilibrium(mixture):
            """Adds the given mixture to the scheduler"""
            if any(linalg.norm(mix - mixture) < 1e-3 for mix in explored_mixtures):
                log.debug('Mixture already explored:\n%s\n', json.dumps(
                    serial.to_prof_json(mixture), indent=2))
            else:
                explored_mixtures.append(mixture)
                log.debug('Exploring equilibrium deviations:\n%s\n', json.dumps(
                    serial.to_prof_json(mixture), indent=2))
                dev = qsched.schedule_deviations(mixture > 0, observation_increment)
                deviations.append((mixture, dev))

        def analyze_subgame(unsched_subgames, sub):
            """Process a subgame"""
            if sub.is_complete():
                subg = sub.get_subgame()
                sub_eqa = nash.mixed_nash(subg, regret_thresh=regret_thresh)
                eqa = subgame.translate(subg.trim_mixture_support(sub_eqa),
                                        sub.subgame_mask)
                if eqa.size == 0:  # No equilibria
                    if sub.counts < reschedule_limit * observation_increment:
                        log.info('Found no equilibria in subgame:\n%s\n', json.dumps(
                            {r: list(s) for r, s in serial.to_prof_json(sub.subgame_mask).items()},
                            indent=2))
                        sub.update_counts(sub.counts + observation_increment)
                        unsched_subgames.append(sub)
                    else:
                        log.error('Failed to find equilibria in subgame:\n%s\n', json.dumps(
                            {r: list(s) for r, s in serial.to_prof_json(subm).items()},
                            indent=2))
                else:
                    log.debug(
                        'Found candidate equilibria:\n%s\nin subgame:\n%s\n',
                        json.dumps(list(map(serial.to_prof_json, eqa)), indent=2),
                        json.dumps({r: list(s) for r, s in serial.to_prof_json(sub.subgame_mask).items()},
                                   indent=2))
                    for eqm in eqa:
                        add_equilibrium(eqm)
            else:
                unsched_subgames.append(sub)

        def analyze_all_deviations(unsched_deviations, mix, dev):
            """Analyzes responses to an equilibrium and book keeps"""
            if dev.is_complete():
                dev_game = dev.get_game()
                responses = regret.mixture_deviation_gains(dev_game, mix,
                                                           assume_complete=True)
                log.debug('Responses:\n%s\nto candidate equilibrium:\n%s\n',
                          json.dumps(serial.to_prof_json(responses, filter_zeros=False), indent=2),
                          json.dumps(serial.to_prof_json(mix), indent=2))

                if np.all(responses < regret_thresh):
                    # found equilibria
                    if not any(linalg.norm(m - mix) < 1e-3 for m in confirmed_equilibria):
                        confirmed_equilibria.append(mix)
                        log.info('Confirmed equilibrium:\n%s\n', json.dumps(
                            serial.to_prof_json(mix), indent=2))

                else:  # Queue up next subgames
                    subsize = dev.subgame_mask.sum()
                    # TODO Normalize role deviations
                    for rstart, role_resps in zip(game.role_starts, game.role_split(responses)):
                        order = np.argpartition(role_resps, role_resps.size-1)
                        gain = role_resps[order[-1]]
                        if gain > 0:
                            # Positive best response exists for role
                            subm = dev.subgame_mask.copy()
                            subm[order[-1] + rstart] = True
                            if subsize < max_subgame_size:
                                add_subgame(subm)
                            else:
                                heapq.heappush(backup, (
                                    (False, False, subsize, -gain),
                                    subm))
                            order = order[:-1]

                        # Priority for backup is (not best response, not beneficial
                        # response, subgame size, deviation loss). Thus, best
                        # responses are first, then positive responses, then small
                        # subgames, then highest gain.

                        # Add the rest to the backup
                        for ind in order:
                            subm = dev.subgame_mask.copy()
                            subm[ind + rstart] = True
                            gain = role_resps[ind]
                            heapq.heappush(backup, (
                                (True, gain < 0, subsize, -gain, id(subm)),
                                subm))
            else:
                unsched_deviations.append((mix, dev))

        # Initialize with pure subgames
        for subm in subgame.pure_subgames(game):
            add_subgame(subm)

        # While still scheduling left to do
        while subgames or deviations:
            if qsched.update():
                # Something finished scheduling
                unsched_subgames = []
                for sub in subgames:
                    analyze_subgame(unsched_subgames, sub)
                subgames = unsched_subgames

                unsched_deviations = []
                for mix, dev in deviations:
                    analyze_all_deviations(unsched_deviations, mix, dev)
                deviations = unsched_deviations

                if (not subgames and not deviations and
                        len(confirmed_equilibria) < required_equilibria):
                    # We've finished all the required stuff, but still haven't
                    # found an equilibrium, so pop a backup off
                    log.debug('Extracting backup game\n')
                    while backup and not subgames:
                        add_subgame(heapq.heappop(backup)[1])

            else:
                # We're still waiting for jobs to complete, so take a break
                log.debug('Waiting %d seconds for simulations to finish...\n',
                          sleep_time)
                time.sleep(sleep_time)

        log.error('Finished quiescing\nConfirmed equilibria:\n%s', json.dumps(
            list(map(serial.to_prof_json, confirmed_equilibria)), indent=2))
        # FIXME dump subgames, equilibria, and game file, failed subgames
        sched.deactivate()

    except KeyboardInterrupt:
        # Manually killed, so just deactivate
        log.error('Manually killed quiesce script. Deactivating scheduler')
        sched.deactivate()


def main():
    """Main function, declared so it doesn't have global scope"""
    # Parse arguments
    args = _parser.parse_args()
    if args.auth_string is None:
        with open(args.auth_file) as auth_file:
            args.auth_string = auth_file.read().strip()

    # Create logger
    log = logging.getLogger(__name__)
    log.setLevel(max(40 - args.verbose * 10, 1))  # 0 is no logging
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s ({gid:d}) %(message)s'.format(gid=args.game)))
    log.addHandler(handler)

    # Email Logging
    if args.recipient:
        email_subject = 'EGTA Online Quiesce Status for Game {gid:d}'.format(
            gid=args.game)
        smtp_host = 'localhost'

        # We need to do this to match the from address to the local host name
        # otherwise, email logging will not work. This seems to vary somewhat
        # by machine
        server = smtplib.SMTP(smtp_host)
        smtp_fromaddr = 'EGTA Online <egta_online@{host}>'.format(
            host=server.local_hostname)
        server.quit()  # dummy server is now useless

        email_handler = handlers.SMTPHandler(smtp_host, smtp_fromaddr,
                                             args.recipient, email_subject)
        email_handler.setLevel(40 - args.email_verbosity * 10)
        log.addHandler(email_handler)

    # Fetch info from egta online
    egta_api = api.EgtaOnline(args.auth_string,
                              logLevel=(0 if args.verbose < 5 else 3))
    gamej = egta_api.game(id=args.game).get_info('summary')
    game, serial = gameio.read_base_game(gamej)
    sim = utils.only(s for s in egta_api.get_simulators()
                     if '{name}-{version}'.format(**s)
                     == gamej.simulator_fullname)

    if args.dpr is not None:
        args.dpr = serial.from_role_json(
            dict(zip(args.dpr[::2], map(int, args.dpr[1::2]))))

    try:
        quiesce(sim, game, serial, gamej.name, dict(gamej.configuration),
                args.dpr, log, max_profiles=args.max_profiles,
                max_subgame_size=args.max_subgame_size,
                sleep_time=args.sleep_time,
                required_equilibria=args.num_equilibria,
                regret_thresh=args.regret_threshold, reschedule_limit=10,
                process_memory=args.memory,
                observation_time=args.observation_time,
                observation_increment=args.observation_increment,
                nodes=args.nodes)

    except Exception as e:
        # Other exception, notify, but don't deactivate
        log.error('Caught exception: (%s) %s\nWith traceback:\n%s\n',
                  e.__class__.__name__, e, traceback.format_exc())
        raise e

    finally:
        # Make sure to clean up
        egta_api.close()


if __name__ == '__main__':
    main()
