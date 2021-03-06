# -*- coding: utf-8 -*-
#
# This file is part of Bika LIMS
#
# Copyright 2011-2017 by it's authors.
# Some rights reserved. See LICENSE.txt, AUTHORS.txt.

from bika.lims import enum
from bika.lims import PMF
from bika.lims.browser import ulocalized_time
from bika.lims.interfaces import IJSONReadExtender
from bika.lims.jsonapi.v1 import get_include_fields
from bika.lims.utils import changeWorkflowState
from bika.lims.utils import t
from bika.lims import logger
from bika.lims import api
from Products.CMFCore.interfaces import IContentish
from Products.CMFCore.WorkflowCore import WorkflowException
from Products.CMFPlone.interfaces import IWorkflowChain
from Products.CMFPlone.workflow import ToolWorkflowChain
from zope.component import adapts
from zope.interface import implementer
from zope.interface import implements
from zope.interface import Interface
from plone import api as ploneapi

def skip(instance, action, peek=False, unskip=False):
    """Returns True if the transition is to be SKIPPED

        peek - True just checks the value, does not set.
        unskip - remove skip key (for manual overrides).

    called with only (instance, action_id), this will set the request variable preventing the
    cascade's from re-transitioning the object and return None.
    """

    uid = callable(instance.UID) and instance.UID() or instance.UID
    skipkey = "%s_%s" % (uid, action)
    if 'workflow_skiplist' not in instance.REQUEST:
        if not peek and not unskip:
            instance.REQUEST['workflow_skiplist'] = [skipkey, ]
    else:
        if skipkey in instance.REQUEST['workflow_skiplist']:
            if unskip:
                instance.REQUEST['workflow_skiplist'].remove(skipkey)
            else:
                return True
        else:
            if not peek and not unskip:
                instance.REQUEST["workflow_skiplist"].append(skipkey)


def doActionFor(instance, action_id):
    actionperformed = False
    message = ''
    if not skip(instance, action_id, peek=True):
        try:
            api.do_transition_for(instance, action_id)
            actionperformed = True
        except ploneapi.exc.InvalidParameterError as e:
            message = str(e)
            logger.warn("Failed to perform transition {} on {}: {}".format(
                action_id, instance, message))
    return actionperformed, message


def BeforeTransitionEventHandler(instance, event):
    """This will run the workflow_before_* on any
    content type that has one.
    """
    # creation doesn't have a 'transition'
    if not event.transition:
        return
    key = 'workflow_before_' + event.transition.id
    method = getattr(instance, key, False)
    if method:
        method()


def AfterTransitionEventHandler(instance, event):
    """This will run the workflow_script_* on any
    content type that has one.
    """
    # creation doesn't have a 'transition'
    if not event.transition:
        return
    key = 'workflow_script_' + event.transition.id
    method = getattr(instance, key, False)
    if method:
        method()


def get_workflow_actions(obj):
    """ Compile a list of possible workflow transitions for this object
    """

    def translate(i):
        return t(PMF(i + "_transition_title"))

    workflow = ploneapi.portal.get_tool("portal_workflow")
    actions = [{"id": it["id"],
                "title": translate(it["id"])}
               for it in workflow.getTransitionsFor(obj)]

    return actions


def isBasicTransitionAllowed(context, permission=None):
    """Most transition guards need to check the same conditions:

    - Is the object active (cancelled or inactive objects can't transition)
    - Has the user a certain permission, required for transition.  This should
    normally be set in the guard_permission in workflow definition.

    """
    workflow = ploneapi.portal.get_tool("portal_workflow")
    mtool = ploneapi.portal.get_tool("portal_membership")
    if workflow.getInfoFor(context, "cancellation_state", "") == "cancelled" \
            or workflow.getInfoFor(context, "inactive_state", "") == "inactive" \
            or (permission and mtool.checkPermission(permission, context)):
        return False
    return True


def getCurrentState(obj, stateflowid):
    """ The current state of the object for the state flow id specified
        Return empty if there's no workflow state for the object and flow id
    """
    workflow = ploneapi.portal.get_tool("portal_workflow")
    return workflow.getInfoFor(obj, stateflowid, '')

def getTransitionDate(obj, action_id):
    workflow = ploneapi.portal.get_tool("portal_workflow")
    try:
        # https://jira.bikalabs.com/browse/LIMS-2242:
        # Sometimes the workflow history is inexplicably missing!
        review_history = list(workflow.getInfoFor(obj, 'review_history'))
    except WorkflowException as e:
        message = str(e)
        logger.error("Cannot retrieve review_history on {}: {}".format(
                obj, message))
        return None
    # invert the list, so we always see the most recent matching event
    review_history.reverse()
    for event in review_history:
        if event['action'] == action_id:
            value = ulocalized_time(event['time'], long_format=True,
                                    time_only=False, context=obj)
            return value
    return None

def getTransitionActor(obj, action_id):
    """Returns the identifier of the user who last performed the action
    on the object.
    """
    workflow = ploneapi.portal.get_tool("portal_workflow")
    try:
        review_history = list(workflow.getInfoFor(obj, "review_history"))
        review_history.reverse()
        for event in review_history:
            if event.get("action") == action_id:
                return event.get("actor")
        return ''
    except WorkflowException as e:
        message = str(e)
        logger.error("Cannot retrieve review_history on {}: {}".format(
            obj, message))
    return ''


# Enumeration of the available status flows
StateFlow = enum(review='review_state',
                 inactive='inactive_state',
                 cancellation='cancellation_state')

# Enumeration of the different available states from the inactive flow
InactiveState = enum(active='active')

# Enumeration of the different states can have a batch
BatchState = enum(open='open',
                  closed='closed',
                  cancelled='cancelled')

BatchTransitions = enum(open='open',
                        close='close')

CancellationState = enum(active='active',
                         cancelled='cancelled')

CancellationTransitions = enum(cancel='cancel',
                               reinstate='reinstate')


class JSONReadExtender(object):

    """- Adds the list of possible transitions to each object, if 'transitions'
    is specified in the include_fields.
    """

    implements(IJSONReadExtender)

    def __init__(self, context):
        self.context = context

    def __call__(self, request, data):
        include_fields = get_include_fields(request)
        if not include_fields or "transitions" in include_fields:
            data['transitions'] = get_workflow_actions(self.context)



@implementer(IWorkflowChain)
def SamplePrepWorkflowChain(ob, wftool):
    """Responsible for inserting the optional sampling preparation workflow
    into the workflow chain for objects with ISamplePrepWorkflow

    This is only done if the object is in 'sample_prep' state in the
    primary workflow (review_state).
    """
    # use catalog to retrieve review_state: getInfoFor causes recursion loop
    chain = list(ToolWorkflowChain(ob, wftool))
    bc = ploneapi.portal.get_tool('bika_catalog')
    proxies = bc(UID=ob.UID())
    if not proxies or proxies[0].review_state != 'sample_prep':
        return chain
    sampleprep_workflow = ob.getPreparationWorkflow()
    if sampleprep_workflow:
        chain.append(sampleprep_workflow)
    return tuple(chain)


def SamplePrepTransitionEventHandler(instance, event):
    """Sample preparation is considered complete when the sampleprep workflow
    reaches a state which has no exit transitions.

    If the stateis state's ID is the same as any AnalysisRequest primary
    workflow ID, then the AnalysisRequest will be sent directly to that state.

    If the final state's ID is not found in the AR workflow, the AR will be
    transitioned to 'sample_received'.
    """
    if not event.transition:
        # creation doesn't have a 'transition'
        return

    if not event.new_state.getTransitions():
        # Is this the final (No exit transitions) state?
        workflow = ploneapi.portal.get_tool("portal_workflow")
        primary_wf_name = list(ToolWorkflowChain(instance, workflow))[0]
        primary_wf = workflow.getWorkflowById(primary_wf_name)
        primary_wf_states = primary_wf.states.keys()
        if event.new_state.id in primary_wf_states:
            # final state name matches review_state in primary workflow:
            dst_state = event.new_state.id
        else:
            # fallback state:
            dst_state = 'sample_received'
        changeWorkflowState(instance, primary_wf_name, dst_state)
