from .. import config, db, http
from ..model import room as mroom, exc, user as muser
from ..web import app
from . import auth

from flask import abort, jsonify, g, Blueprint, request, make_response, Response
from werkzeug.http import http_date, parse_options_header
from os import path, fstat
import urllib.parse

# Room-related routes, excluding retrieving/posting messages


rooms = Blueprint('rooms', __name__)


def get_room_info(room):
    mods, admins, h_mods, h_admins = room.get_mods(g.user)

    rr = {
        'token': room.token,
        'name': room.name,
        'info_updates': room.info_updates,
        'message_sequence': room.message_sequence,
        'created': room.created,
        'active_users': room.active_users(),
        'active_users_cutoff': int(config.ROOM_DEFAULT_ACTIVE_THRESHOLD),
        'moderators': mods,
        'admins': admins,
        'read': room.check_read(g.user),
        'write': room.check_write(g.user),
        'upload': room.check_upload(g.user),
    }

    if room.description is not None:
        rr['description'] = room.description

    if room.image_id is not None:
        rr['image_id'] = room.image_id

    pinned = room.pinned_messages
    if pinned:
        rr['pinned_messages'] = pinned

    if h_mods:
        rr['hidden_moderators'] = h_mods
    if h_admins:
        rr['hidden_admins'] = h_admins

    if room.check_moderator(g.user):
        rr['moderator'] = True
        rr['default_read'] = room.default_read
        rr['default_accessible'] = room.default_accessible
        rr['default_write'] = room.default_write
        rr['default_upload'] = room.default_upload
    if room.check_admin(g.user):
        rr['admin'] = True
    if g.user:
        if g.user.global_moderator:
            rr['global_moderator'] = True
        if g.user.global_admin:
            rr['global_admin'] = True

    return rr


@rooms.get("/room/<Room:room>")
@auth.accessible_required
def get_one_room(room):
    """
    Returns the details of a single room.

    # Return value

    A JSON object with keys:

    - `token` — The room token as used in a URL, e.g. `"sudoku"`.
    - `name` — The room name typically shown to users, e.g. `"Sodoku Solvers"`.
    - `description` — Text description of the room, e.g. `"All the best sodoku discussion!"`.
    - `info_updates` — Monotonic integer counter that increases whenever the room's metadata changes
    - `message_sequence` — Monotonic room post counter that increases each time a message is posted,
      edited, or deleted in this room.  (Note that changes to this field do *not* imply an update
      the room's `info_updates` value, nor vice versa).
    - `created` — Unix timestamp (as a float) of the room creation time.  Note that unlike earlier
      versions of SOGS, this is a proper seconds-since-epoch unix timestamp, not a javascript-style
      millisecond value.
    - `active_users` — Number of recently active users in the room over a recent time period (as
      given in the `active_users_cutoff` value).  Users are considered "active" if they have
      accessed the room (checking for new messages, etc.) at least once in the given period.
      **Note:** changes to this field do *not* update the room's `info_updates` value.
    - `active_users_cutoff` — The length of time (in seconds) of the `active_users` period.
      Defaults to a week (604800), but the open group administrator can configure it.
    - `image_id` — File ID of an uploaded file containing the room's image.  Omitted if there is no
      image.
    - `pinned_messages` — Array of pinned message information (omitted entirely if there are no
      pinned messages).  Each array element is an object with keys:
        * `id` — The numeric message id.
        * `pinned_at` — The unix timestamp when the message was pinned.
        * `pinned_by` — The session ID of the admin who pinned this message (which is not
          necessarily the same as the author of the message).
    - `moderators` — Array of Session IDs of the room's publicly viewable moderators.  This does not
      include room administrators nor hidden moderators.
    - `admins` — Array of Session IDs of the room's publicly viewable moderators.  This does not
      include room moderator nor hidden admins.
    - `hidden_moderators` — Array of Session IDs of the room's publicly hidden moderators.  This
      field is only included if the requesting user has moderator or admin permissions, and is
      omitted if empty.
    - `hidden_admins` — Array of Session IDs of the room's publicly hidden admins.  This field is
      only included if the requesting user has moderator or admin permissions, and is omitted if
      empty.
    - `default_read`, `default_accessible`, `default_write`, `default_upload` — These four boolean
      fields indicate whether new users have read, access, write, and upload permissions,
      respectively, in the room.  They are included in the response only if the requesting user has
      moderator or admin permissions.
    - `read`, `write`, `upload` — These three boolean flags indicate whether the **current** user
      has permission to read messages, write messages, or upload files to this room, respectively.
      (Accessibility is not included as being able to access the room information at all implies the
      room is accessible).
    - `moderator` — True if the current user has moderator permissions in the room, omitted
      otherwise.
    - `admin` — True if the current user has admin permissions in the room, omitted otherwise.  This
      is *not* exclusive of `moderator`: that is, an admin will have both `admin` and `moderator`
      set to true.
    - `global_moderator` — True if the current user is a global moderator.  This is not exclusive of
      `moderator`: a global moderator will have both flags set.
    - `global_admin` — True if the current user is a global admin.  This is *not* exclusive of
      `global_moderator`/`moderator`/`admin`: that is, a global admin will have all four set to
      true.

    # Access permissions

    The four access permissions control what a user can do in a room.  Users can have specific
    overrides (either true or false) applied for each room by moderators; if there are no such
    override then a user receives the room's `default_*` permission (e.g. `default_read`).  The
    meaning of each permission is as follows:

    - `read` — this allows a user to read messages posted in the room.
    - `write` — this allows users to post messages to the room.
    - `upload` — this allows users to upload attachments to the room (but only if `write` is also
      set).
    - `access` — this flag controls only applies when a user does *not* have `read` access: if this
      is true (which is the default for new rooms) then the user can still see information about the
      room such as its name, description, and user count, but cannot access the messages themselves.
      If this is *false* then the user does not have any access to the room at all and will receive
      a 404 Not Found error if attempting to access it (the same thing they would see if the room
      didn't exist).  This is provided to allow for "secret" rooms that only invited users may
      access (by setting both `default_accessible` and `default_write` to false).

    # Error status codes

    - 403 Forbidden — Returned if the current user does not have permission to access the room,
      e.g. because they are banned or the room permissions otherwise restrict access.

    - 404 Not Found — Returned if the room does not exist, or is configured as inaccessible (and
      this user doesn't have access).
    """
    return jsonify(get_room_info(room))


@rooms.get("/rooms")
def get_rooms():
    """
    Returns a list of available rooms on the server.

    Rooms to which the user does not have access (e.g. because they are banned, or the room has
    restricted access permissions) are not included.

    # Return value

    Returns a json list of the rooms.  Each room is an JSON object as would be returned by [the
    single-room version](#get-roomroom) of this call.
    """
    return jsonify([get_room_info(room=r) for r in mroom.get_accessible_rooms(g.user)])


BAD_NAME_CHARS = {c: None for c in range(32)}
BAD_DESCRIPTION_CHARS = {c: None for c in range(32) if not (0x09 <= c <= 0x0A)}


@rooms.put("/room/<Room:room>")
@auth.admin_required
def update_room(room):
    """
    Updates room details/settings.

    This request takes a JSON object as request body containing the room details to update.  Any
    field can be omitted to leave it at its current value.  The invoking user must have admin
    permissions in the room to call this method.

    Supported fields are:

    - `name` — New user-displayed single-line name/title of this room.  UTF-8 encoded; newlines,
      tabs and other control characters (i.e. all codepoints below \u0020) will be stripped out.
    - `description` — Long description to show to users, typically in smaller text below the room
      name.  UTF-8 encoded, and permits newlines, tabs; other control characters below \u0020 will
      be stripped out.  Can be `null` or an empty string to remove the description entirely.
    - `default_read`, `default_accessible`, `default_write`, `default_upload` — if specified these
      update the room's default read, access, write, and upload permissions for ordinary users (i.e.
      users who do not have any other user-specific permission applied).  See the description of
      Access permissions in the (room information)[#get-roomroom] endpoint for details.
    - `image` — The file id of an image that was uploaded in this room to use as the room icon.

    # Return value

    On success this endpoint returns a 200 status code and an empty json object (`{}`) as the body.

    # Error status codes

    - 403 Forbidden — if the invoking user does not have administrator access to the room.

    - 406 Not Acceptable — if the given data is not acceptable.  Currently this response occurs if a
      given `image` is invalid (i.e. does not exist, or is not uploaded to this room).
    """

    req = request.json

    with db.transaction():
        did = False
        if 'name' in req:
            n = req['name']
            if not isinstance(n, str):
                app.logger.warning(f"Room update with invalid name: {type(n)} != str")
                abort(http.BAD_REQUEST)
            room.name = n.translate(BAD_NAME_CHARS)
            did = True
        if 'description' in req:
            d = req['description']
            if not (d is None or isinstance(d, str)):
                app.logger.warning(f"Room update: invalid description: {type(d)} is not str, null")
                abort(http.BAD_REQUEST)
            if d is not None:
                d = d.translate(BAD_DESCRIPTION_CHARS)
                if len(d) == 0:
                    d = None

            room.description = d
            did = True
        read, accessible, write, upload = (
            req.get('default_' + x) for x in ('read', 'accessible', 'write', 'upload')
        )
        if 'image' in req:
            img = req.get('image')
            if not isinstance(img, int):
                app.logger.warning(f"Room update: invalid image: {type(id)} is not an integer")
                abort(http.BAD_REQUEST)
            try:
                room.image = img
            except exc.NoSuchFile as e:
                app.logger.warning(f"Room image update invalid: {e}")
                abort(http.NOT_ACCEPTABLE)
            did = True

        for val in (read, accessible, write, upload):
            if not (val is None or isinstance(val, bool) or isinstance(val, int)):
                app.logger.warning(
                    "Room update: default_read/accessible/write/upload must be bool, not "
                    f"{type(val)}"
                )
                abort(http.BAD_REQUEST)

        if read is not None:
            room.default_read = bool(read)
            did = True
        if accessible is not None:
            room.default_accessible = bool(accessible)
            did = True
        if write is not None:
            room.default_write = bool(write)
            did = True
        if upload is not None:
            room.default_upload = bool(upload)
            did = True

        if not did:
            app.logger.warning("Room update: must include at least one field to update")
            abort(http.BAD_REQUEST)

    return jsonify({})


def addExtraPermInfo(perms):
    """ """
    if perms.get("moderator"):
        perms["hidden"] = not bool(perms.pop("visible_mod"))
    if perms.get("admin"):
        del perms["moderator"]
    # if banned is explicitly provided and set to false omit it entirely
    if perms.get("banned") is False:
        del perms["banned"]
    return perms


@rooms.get("/room/<Room:room>/permInfo")
@auth.mod_required
def get_permission_info(room):
    """
    Fetches permissions about the room, like ban info etc.

    # Return Value

    dict of session_id to current permissions,
    a dict containing the name of the permission mapped to a boolean value.
    """
    perms = room.permissions
    return jsonify({key: addExtraPermInfo(perms[key]) for key in perms.keys()})


@rooms.get("/room/<Room:room>/futurePermInfo")
@auth.mod_required
def get_future_permission_info(room):
    """
    Fetches permission changes scheduled in the future.

    # Return Value

    list of all future permission changes scheduled

    """

    return jsonify(room.future_permissions)


@rooms.get("/room/<Room:room>/pollInfo/<int:info_updated>")
@auth.read_required
def poll_room_info(room, info_updated):
    """
    Polls a room for metadata updates.

    The endpoint polls room metadata for this room, always including the instantaneous room details
    (such as the user's permission and current number of active users), and including the full room
    metadata if the room's info_updated counter has changed from the provided value.

    # URL Parameters

    - `info_updated` — The client's currently cached `info_updates` value for the room.  The full
      room metadata is returned in the response if and only if the room's last update count does not
      equal the given value.

    # Return value

    On success this returns the results of polling the room for updated information.  This endpoint
    always returns ephemeral data, such as the number of active users and the current user's
    permissions, and will include the full room details if and only if it has changed (i.e.
    info_updates does not match) from the `info_updated` value provided by the requestor.

    Note that the `details` field is only present and populated if the room's `info_updates` counter
    differs from the provided `info_updated` value; otherwise the values are unchanged and so it is
    omitted.

    The return value is a JSON object containing the following subset of values of [the full room
    details](#get-roomroom):

    - `token`
    - `active_users`
    - `read`, `write`, `upload`
    - `moderator`, `admin`, `global_moderator`, `global_admin`
    - `default_read`, `default_accessible`, `default_write`, `default_upload`

    If the room metadata has changed then the following is also included:

    - `details` — The full room metadata (as would be returned by the [`/rooms/ROOM`
      endpoint](#get-roomroom)).

    The intention is that this endpoint allows a client to know that it doesn't need to worry about
    updating the room image or pinned messages whenever the `details` field is not included in the
    response.

    # Error status codes

    - 403 Forbidden — if the invoking user does not have access to the room.
    """
    if g.user:
        g.user.update_room_activity(room)

    result = {
        'token': room.token,
        'active_users': room.active_users(),
        'read': room.check_read(g.user),
        'write': room.check_write(g.user),
        'upload': room.check_upload(g.user),
    }

    if room.info_updates != info_updated:
        result['details'] = get_room_info(room)

    if room.check_moderator(g.user):
        result['moderator'] = True
        result['default_read'] = room.default_read
        result['default_accessible'] = room.default_accessible
        result['default_write'] = room.default_write
        result['default_upload'] = room.default_upload
    if room.check_admin(g.user):
        result['admin'] = True
    if g.user:
        if g.user.global_moderator:
            result['global_moderator'] = True
        if g.user.global_admin:
            result['global_admin'] = True

    return jsonify(result)


@rooms.post("/room/<Room:room>/file")
@auth.user_required
def upload_file(room):
    """
    Uploads a file to a room.

    Takes the request as binary in the body and takes other properties (specifically the suggested
    filename) via submitted headers.

    The user must have upload and posting permissions for the room.  The file will have a default
    lifetime of 1 hour, which is extended to 15 days (by default) when a post referencing the
    uploaded file is posted or edited.

    # URL Parameters

    # Body

    The body of the request is the raw bytes that make up the attachment body.

    # Header parameters

    ## Content-Type

    This should be set to application/octet-stream.  If the client has a strong reason to use
    another content type then it may do so, but it is acceptable to always use
    `application/octet-stream`.

    ## Content-Disposition

    The attachment filename should be provided via the `Content-Disposition` header of the request,
    encoded as URI-encoded UTF-8 as per RFC 5987.  Specifically, it should be formatted as:

        Content-Disposition: attachment; filename*=UTF-8''filename.txt

    where `filename.txt` is a utf-8 byte sequence with any bytes not in the following list encoded
    using %xx URI-style encoding.

    Non-encoded ascii characters: A-Z, a-z, 0-9, !, #, $, &, +, -, ., ^, _, `, |, ~.  All other
    characters shall be represented as their utf-8 byte sequence.

    For instance, a file named `my 🎂.txt` (🎂 = U+1F382, with utf-8 representation 0xF0 0x9F 0x8E
    0x82) should specify the filename in the header as:

        Content-Disposition: attachment; filename*=UTF-8''my%20%f0%9f%8e%82.txt

    Filenames are not required as they are not always available (such as when uploading a pasted
    image) but should be used when possible.

    The filename, if provided, will be provided in the same format in the download header for the
    file.

    # Error status codes

    - 403 Forbidden — Returned if the current user does not have permission to post messages or
      upload files to the room.

    - 404 Not Found — Returned if the room does not exist, or is configured as inaccessible (and
      this user doesn't have access).

    # Return value

    On successful upload this endpoint returns a 201 (Created) status code (*not* 200), with a JSON
    body containing an object with key:

    - `id` — the numeric id of the upload.  If the id is not referenced via a subsequent new post,
      post edit, or room image request within one hour then the attachment will be deleted.
    """

    if not room.check_upload(g.user):
        abort(http.FORBIDDEN)

    filename = None
    # parse filename, this is god awful
    for k, v in request.headers:
        if k.lower() == 'content-disposition':
            cd = parse_options_header(v)
            if len(cd) == 2 and 'filename' in cd[1]:
                filename = cd[1]['filename']

    # 1 hour lifetime before link to post
    id = room.upload_file(request.data, g.user, filename=filename, lifetime=3600.0)
    resp = make_response(jsonify({"id": id}))
    resp.status_code = http.CREATED
    return resp


@rooms.get("/room/<Room:room>/file/<int:fileId>")
@auth.read_required
def serve_file(room, fileId):
    """
    Retrieves a file uploaded to the room.

    Retrieves a file via its numeric id from the room, returning the file content directly as the
    binary response body.  The file's suggested filename (as provided by the uploader) is provided
    in the Content-Disposition header, if available.

    # URL Parameters

    - `fileId` — The id of the attachment to download.

    # Return value

    On success the file content is returned as bytes in the response body.  Additional information
    is provided via response headers:

    ## Content-Length

    The size (in bytes) of the attachment.

    ## Content-Type

    Always `application-octet-stream` (even if the uploader specified something else).

    ## Content-Disposition

    This specifies the suggested filename as provided by the uploader, if present.  The filename is
    encoded using standard RFC 5987 encoding, for example:

        Content-Disposition: attachment; filename*=UTF-8''filename.txt

    See [the upload endpoint](#post-roomroomfile) for filename encoding details.  If the attachment
    was uploaded without a filename then this header will not include the filename component, i.e.:

        Content-Disposition: attachment

    ## Date

    The timestamp at which this file was uploaded, as a standard HTTP date.

    ## Expires

    The timestamp at which this file is currently scheduled to expire, as a standard HTTP date.

    # Error status codes

    - 403 Forbidden — Returned if the current user does not have permission to read messages in the
      room, e.g. because they are banned or the room permissions otherwise restrict access.

    - 404 Not Found — Returned if the attachment does not exist in this room (or has expired).
    """
    room_file = room.get_file(fileId)
    if not room_file:
        abort(http.NOT_FOUND)

    f = open(path.join(path.abspath(path.curdir), room_file.path), 'rb')

    headers = {
        'Date': http_date(room_file.uploaded),
        'Content-Length': fstat(f.fileno()).st_size,
        'Content-Disposition': 'attachment',
    }
    if room_file.filename:
        headers['Content-Disposition'] = "attachment; filename*=UTF-8''{}".format(
            urllib.parse.quote(room_file.filename.encode('utf-8'))
        )
    if room_file.expiry:
        headers["Expires"] = http_date(room_file.expiry)

    return Response(
        response=f, status=200, content_type='application/octet-stream', headers=headers
    )


@rooms.get("/room/<Room:room>/file/<int:fileId>/<filename>")
@auth.read_required
def serve_file_with_ignored_filename(room, fileId, filename):
    """
    Convenience endpoint for downloading file with a filename appended to the URL.

    This endpoint is exactly identical to the version of the endpoint without a filename: the
    suffixed filename in the request is simply ignored.  This alias is provided only to make it
    slightly more convenient to construct a URL containing a known filename, such as when using
    command-line tools for debugging.

    Most clients should simply use the non-suffixed endpoint instead.

    # URL Parameters

    - `fileId` — The id of the attachment to download.

    - `filename` — Arbitrary filename of the attachment; this value is entirely ignored by SOGS.

    """
    return serve_file(room=room, fileId=fileId)


@rooms.delete("/room/<Room:room>/all/<SessionID:sid>")
def delete_all_posts(room, sid):
    """
    Deletes all posts from a room made by a user

    # URL Parameters

    - `sid` — the session id of the user to ban

    # Return value

    An empty json object is returned.

    # Error status codes

    - 403 Forbidden — if the invoking user does not have access to the room.
    - 404 Not Found — if the user we are deleting posts from made no posts in this room.
    """
    user = muser.User(session_id=sid, autovivify=False)
    deleted, _ = room.delete_all_posts(user, deleter=g.user)
    if not deleted:
        abort(http.NOT_FOUND)
    return jsonify({})
