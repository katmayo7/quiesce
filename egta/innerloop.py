"""module for doing innerloop procedures"""
import asyncio
import functools
import heapq
import logging

import numpy as np
from gameanalysis import collect
from gameanalysis import nash
from gameanalysis import regret
from gameanalysis import restrict

import sys


# TODO There's something to be said about individual rationality constraints
# relative to best deviation, i.e. if all are positive gains, but negative
# payoff, that might mean we should warn, or at least explore something else.
# TODO Restrict size should maybe be a num strats per role and set such that
# and restriction that has less than the number of profiles is valid. For
# backwards compatibility, it'd be nice to have a good way to interpret a
# single number for games with multiple roles, potentially the division that
# produces the maximum number of profiles?
# TODO Schedule restriction should only schedule if equilibria < num or haven't
# started queuing backups
# TODO It'd be cool to have an option to explore better responses proportional
# to gain instead of best response. This would require a deterministic random
# seed in order to get consistent output.
# TODO in failure conditions, some tasks are left hanging, instead we should be
# cancelling, and awaiting them so they're not left running
async def inner_loop(  # pylint: disable=too-many-locals
    agame,
    *,
    initial_restrictions=None,
    regret_thresh=1e-3,
    dist_thresh=0.1,
    support_thresh=1e-4,
    restricted_game_size=3,
    num_equilibria=1,
    num_backups=1,
    devs_by_role=False,
    style="best",
    executor=None
):
    """Inner loop a game using a scheduler

    Parameters
    ----------
    game : RsGame
        The game to find equilibria in. This function is most useful when game
        is a SchedulerGame, but any complete RsGame will work.
    initial_restriction : [[bool]], optional
        Initial restrictions to start inner loop from. If unspecified, every
        pure restriction is used.
    regret_thresh : float > 0, optional
        The maximum regret to consider an equilibrium an equilibrium.
    dist_thresh : float > 0, optional
        The minimum norm between two mixed profiles for them to be considered
        distinct.
    support_thresh : float > 0, optional
        Candidate equilibria strategies with probability lower than this will
        be truncated. This is useful because often Nash finding returns
        strategies with very low support, which now mean extra deviating
        strategies need to be sampled. Trimming saves these samples, but may
        increase regret above the threshold.
    restricted_game_size : int > 0, optional
        The maximum restricted game support size with which beneficial
        deviations must be explored. Restricted games with support larger than
        this are queued and only explored in the event that no equilibrium can
        be found in beneficial deviations smaller than this.
    num_equilibria : int > 0, optional
        The number of equilibria to attempt to find. Only one is guaranteed,
        but this might be beneifical if the game has a known degenerate
        equilibria, but one which is still helpful as a deviating strategy.
    num_backups : int > 0, optional
        In the event that no equilibrium can be found in beneficial deviations
        to small restricted games, other restrictions will be explored. This
        parameter indicates how many restricted games for each role should be
        explored.
    devs_by_role : boolean, optional
        If specified, deviations will only be explored for each role in order,
        proceeding to the next role only when no beneficial deviations are
        found. This can reduce the number of profiles sampled, but may also
        fail to find certain equilibria due to the different path through
        restricted games.
    style : string, optional
        A string describing the thoroughness of equilibrium finding. Seed
        `nash.mixed_equilibria` for options and a description.
    executor : Executor, optional
        The executor to be used for Nash finding. The default setting will
        allow async networking calls to continue to happen during long nash
        finding, but buy using a process pool this can take advantage of
        parallel computation.
    """
    init_role_dev = 0 if devs_by_role else None

    exp_restrictions = collect.bitset(agame.num_strats)
    backups = [[] for _ in range(agame.num_roles)]
    equilibria = collect.mcces(dist_thresh)
    loop = asyncio.get_event_loop()

    #all_profiles = set() #all unique profiles seen (for profile counting)
    all_max_games = set() #all maximal games seen (for profile counting via games)
    all_deviations = set() #all deviation profiles seen not part of a completed maximal subgame
    eq_num_profiles = {} #{equilibrium: (regret, # unique profiles encounted so far)}

    """
    helper function for counting the number of profiles encountered
    --adds game if not represented in the game set
    --if maximal subgame of games already in the set, removes such games
    """
    def add_subgame(rest):
        add_to_list = False
        min_games = []

        if len(all_max_games) == 0:
            add_to_list = True
        else:
            for m in all_max_games:
                temp = m[0]
                diff = (rest & temp)
                if np.sum(rest) == np.sum(diff) and np.sum(temp) >= np.sum(rest):
                    break
                else:
                    if np.sum(diff) != 0:
                        min_games.append(m)
                    add_to_list = True

        if add_to_list == True:
            for m in min_games:
                all_max_games.remove(m)
            to_add = tuple(rest)
            n = len(agame._rprofs(rest))
            all_max_games.add(tuple([to_add, n]))
            #check if adding this game removes anything from deviations list
            check_remove_deviations(rest)

    """
    helper function for counting the number of profiles encountered
    --removes profiles from the deviation set that are captured by the restricted game rest
    """
    def check_remove_deviations(rest):
        to_remove = set()

        for dev in all_deviations:
            temp = [d != 0 for d in dev]
            diff = (rest & temp)
            if np.sum(temp) == np.sum(diff):
                to_remove.add(tuple(dev))

        for r in to_remove:
            all_deviations.remove(r)

    """
    helper function for counting the number of profiles encountered
    --adds deviations not covered by a restricted game in the game set to a separate set of deviation profiles
    --allows for tracking of profiles that are encountered in checking deviations, but whose subgames have not been fully explored yet
    """
    def check_add_deviations(deviations):
        for dev in deviations:
            covered = False
            temp_dev = [d != 0 for d in dev]

            for m in all_max_games:
                diff = (np.array(m[0]) & temp_dev)
                if np.sum(temp_dev) == np.sum(diff):
                    covered = True 

            if covered == False:
                all_deviations.add(tuple(dev))

    """
    helper function for counting the number of profiles encountered
    --get a count of the number of profiles encountered so far
    --uses the number of profiles in the completed maximal subgames and the number of deviation profiles
    """
    def count_profiles():
        num_profiles = 0
        for m in all_max_games:
            num_profiles += m[1]
        num_profiles += len(all_deviations)

        return num_profiles

    async def add_restriction(rest):
        """Adds a restriction to be evaluated"""
        if not exp_restrictions.add(rest):
            return  # already explored
        if agame.is_pure_restriction(rest):
            # Short circuit for pure restriction
            add_subgame(rest) #count games
            return await add_deviations(rest, rest.astype(float), init_role_dev)
        data = await agame.get_restricted_game(rest)

        #count number of profiles (egta schedgame.py)
        #profs = agame._rprofs(rest)
        #for p in profs:
            #temp_p = tuple(p)
            #all_profiles.add(temp_p)
        add_subgame(rest) #count games

        reqa = await loop.run_in_executor(
            executor,
            functools.partial(
                nash.mixed_equilibria,
                data,
                regret_thresh=regret_thresh,
                dist_thresh=dist_thresh,
                style=style,
                processes=1,
            ),
        )
        if reqa.size:
            eqa = restrict.translate(
                data.trim_mixture_support(reqa, thresh=support_thresh), rest
            )
            await asyncio.gather(
                *[add_deviations(rest, eqm, init_role_dev) for eqm in eqa]
            )
        else:
            logging.warning(
                "couldn't find equilibria in %s with restriction %s. This is "
                "likely due to high variance in payoffs which means "
                "quiesce should be re-run with more samples per profile. "
                "This could also be fixed by performing a more expensive "
                "equilibria search to always return one.",
                agame,
                agame.restriction_to_repr(rest),
            )

    async def add_deviations(rest, mix, role_index):
        """Add deviations to be evaluated"""
        # We need the restriction here, since trimming support may increase
        # regret of strategies in the initial restriction
        data = await agame.get_deviation_game(mix > 0, role_index)
        devs = data.deviation_payoffs(mix)

        #count number of profiles
        profs = data.profiles()
        #for p in profs:
            #temp_p = tuple(p)
            #all_profiles.add(temp_p)
        #for counting games
        #check_add_deviations(profs)

        exp = np.add.reduceat(devs * mix, agame.role_starts)
        gains = devs - exp.repeat(agame.num_role_strats)
        if role_index is None:
            if np.all((gains <= regret_thresh) | rest):
                # Found equilibrium
                reg = gains.max()
                if equilibria.add(mix, reg):
                    #logging.warning(
                        #"found equilibrium %s in game %s with regret %f",
                        #agame.mixture_to_repr(mix),
                        #agame,
                        #reg,
                    #)
                    #log # profiles with regret
                    if agame.mixture_to_repr(mix) not in eq_num_profiles:
                        #get number fo profiles
                        #temp_num_profiles = len(all_profiles)
                        temp_num_profiles = count_profiles()
                        eq_num_profiles[agame.mixture_to_repr(mix)] = (reg, temp_num_profiles)
                        #logging.warning('number of profiles: %d', temp_num_profiles)
            else:
                await asyncio.gather(
                    *[
                        queue_restrictions(rgains, ri, rest)
                        for ri, rgains in enumerate(
                            np.split(gains, agame.role_starts[1:])
                        )
                    ]
                )

        else:  # Set role index
            rgains = np.split(gains, agame.role_starts[1:])[role_index]
            rrest = np.split(rest, agame.role_starts[1:])[role_index]
            if np.all((rgains <= regret_thresh) | rrest):  # No deviations
                role_index += 1
                if role_index < agame.num_roles:  # Explore next deviation
                    await add_deviations(rest, mix, role_index)
                else:  # found equilibrium
                    # This should not require scheduling as to get here all
                    # deviations have to be scheduled
                    data = await agame.get_deviation_game(mix > 0)
                    reg = regret.mixture_regret(data, mix)
                    if equilibria.add(mix, reg):
                        #logging.warning(
                            #"found equilibrium %s in game %s with regret %f",
                            #agame.mixture_to_repr(mix),
                            #agame,
                            #reg,
                        #)
                        #log # profiles with regret
                        if agame.mixture_to_repr(mix) not in eq_num_profiles:
                            #temp_num_profiles = len(all_profiles)
                            temp_num_profiles = count_profiles()
                            eq_num_profiles[agame.mixture_to_repr(mix)] = (reg, temp_num_profiles)
                            #logging.warning('number of profiles: %d', temp_num_profiles)
            else:
                await queue_restrictions(rgains, role_index, rest)

    async def queue_restrictions(role_gains, role_index, rest):
        """Queue new restrictions appropriately"""
        role_rest = np.split(rest, agame.role_starts[1:])[role_index]
        if role_rest.all():
            return  # Can't deviate

        rest_size = rest.sum()
        role_start = agame.role_starts[role_index]

        best_resp = np.nanargmax(np.where(role_rest, np.nan, role_gains))
        if role_gains[best_resp] > regret_thresh and rest_size < restricted_game_size:
            br_sub = rest.copy()
            br_sub[role_start + best_resp] = True
            await add_restriction(br_sub)
        else:
            best_resp = None  # Add best response to backup

        back = backups[role_index]
        for strat_ind, (gain, role) in enumerate(zip(role_gains, role_rest)):
            if strat_ind == best_resp or role or gain <= 0:
                continue
            sub = rest.copy()
            sub[role_start + strat_ind] = True
            heapq.heappush(back, (-gain, id(sub), sub))  # id for tie-breaking


    restrictions = (
        agame.pure_restrictions()
        if initial_restrictions is None
        else np.asarray(initial_restrictions, bool)
    )

    iteration = 0
    while len(equilibria) < num_equilibria and (
        any(q for q in backups) or not next(iter(exp_restrictions)).all()
    ):
        if iteration == 1:
            logging.warning(
                "scheduling backup restrictions in game %s. This only happens "
                "when quiesce criteria could not be met with current "
                "maximum restriction size (%d). This probably means that "
                "the maximum restriction size should be increased. If "
                "this is happening frequently, increasing the number of "
                "backups taken at a time might be desired (currently %s).",
                agame,
                restricted_game_size,
                num_backups,
            )
        elif iteration > 1:
            logging.info("scheduling backup restrictions in game %s", agame)

        await asyncio.gather(*[add_restriction(r) for r in restrictions])

        restrictions = collect.bitset(agame.num_strats, exp_restrictions)
        for role, back in enumerate(backups):
            unscheduled = num_backups
            while unscheduled > 0 and back:
                rest = heapq.heappop(back)[-1]
                unscheduled -= restrictions.add(rest)
            for _ in range(unscheduled):
                added = False
                for mask in restrictions:
                    rmask = np.split(mask, agame.role_starts[1:])[role]
                    if rmask.all():
                        continue
                    rest = mask.copy()
                    # TODO We could randomize instead of taking the first
                    # strategy, but this would remove reproducability unless it
                    # was somehow predicated on all of the explored
                    # restrictions or something...
                    strat = np.split(rest, agame.role_starts[1:])[role].argmin()
                    rest[agame.role_starts[role] + strat] = True
                    restrictions.add(rest)
                    added = True
                    break
                if not added:
                    break
        iteration += 1

    # Return equilibria
    if equilibria:  # pylint: disable=no-else-return
        return np.stack([eqm for eqm, _ in equilibria]),eq_num_profiles
    else:
        return np.empty((0, agame.num_strats)),eq_num_profiles  # pragma: no cover
