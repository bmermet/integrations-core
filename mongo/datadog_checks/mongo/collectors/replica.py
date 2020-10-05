import time

from six.moves.urllib.parse import urlsplit

from datadog_checks.mongo.collectors.base import MongoCollector
from datadog_checks.mongo.common import SOURCE_TYPE_NAME, get_long_state_name, get_state_name

try:
    import datadog_agent
except ImportError:
    from datadog_checks.base.stubs import datadog_agent


class ReplicaCollector(MongoCollector):
    """Collect replica set metrics by running the replSetGetStatus command. Also keep track of the previous node state
    in order to submit events on any status change.
    """

    def __init__(self, check, tags):
        super(ReplicaCollector, self).__init__(check, tags)
        self._last_states = check.last_states_by_server
        self.hostname = self.extract_hostname_for_event(self.check.clean_server_name)

    @staticmethod
    def extract_hostname_for_event(server_uri):
        """Make a reasonable hostname for a replset membership event to mention."""
        uri = urlsplit(server_uri)
        if '@' in uri.netloc:
            hostname = uri.netloc.split('@')[1].split(':')[0]
        else:
            hostname = uri.netloc.split(':')[0]
        if hostname == 'localhost':
            hostname = datadog_agent.get_hostname()

        return hostname

    def _report_replica_set_states(self, members, replset_name):
        """
        Report the all members' state changes in the replica set.
        This method only runs on the primary.
        """

        for member in members:
            # The id field cannot be changed for a given replica set member.
            member_id = member['_id']
            status_id = member['state']
            old_state = self.check.last_states_by_server.get(member_id)
            if not old_state:
                # First time the agent sees this replica set member.
                continue

            if old_state == status_id:
                continue
            previous_short_state_str = get_state_name(old_state)
            short_state_str = get_state_name(status_id)
            status_long_str = get_long_state_name(status_id)
            node_hostname = member['name']

            msg_title = "{} is {} for {}".format(node_hostname, short_state_str, replset_name)
            msg = (
                "MongoDB {node} (_id: {id}, {uri}) just reported as {status} ({status_short}) "
                "for {replset_name}; it was {old_state} before.".format(
                    node=node_hostname,
                    id=member_id,
                    uri=self.check.clean_server_name,
                    status=status_long_str,
                    status_short=short_state_str,
                    replset_name=replset_name,
                    old_state=previous_short_state_str,
                )
            )

            event_payload = {
                'timestamp': int(time.time()),
                'source_type_name': SOURCE_TYPE_NAME,
                'msg_title': msg_title,
                'msg_text': msg,
                'host': node_hostname,
                'tags': [
                    'action:mongo_replset_member_status_change',
                    'member_status:' + short_state_str,
                    'previous_member_status:' + previous_short_state_str,
                    'replset:' + replset_name,
                ],
            }
            if node_hostname == 'localhost':
                # Do not submit events with a 'localhost' hostname.
                event_payload['host'] = node_hostname
            self.check.event(event_payload)

    def collect(self, client):
        db = client["admin"]
        status = db.command('replSetGetStatus')
        result = {}

        # Find nodes: current node (ourself) and the primary
        current = primary = None
        is_primary = False
        for member in status.get('members'):
            if member.get('self'):
                current = member
                if int(member.get('state')) == 1:
                    is_primary = True
            if int(member.get('state')) == 1:
                primary = member

        # Compute a lag time
        if current is not None and primary is not None:
            if 'optimeDate' in primary and 'optimeDate' in current:
                lag = primary['optimeDate'] - current['optimeDate']
                result['replicationLag'] = lag.total_seconds()

        if current is not None:
            result['health'] = current['health']

        # Collect the number of votes
        config = db.command('replSetGetConfig')
        votes = 0
        total = 0.0
        for member in config['config']['members']:
            total += member.get('votes', 1)
            if member['_id'] == current['_id']:
                votes = member.get('votes', 1)
        result['votes'] = votes
        result['voteFraction'] = votes / total
        result['state'] = status['myState']
        self._submit_payload({'replSet': result})

        if is_primary:
            # Submit events
            replset_name = status['set']
            self._report_replica_set_states(status['members'], replset_name)

        self.check.last_states_by_server = {member['_id']: member['state'] for member in status['members']}
