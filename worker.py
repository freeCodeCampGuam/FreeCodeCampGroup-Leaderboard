import asyncio
from utils.jsonIO import *
import os
import aiohttp
import datetime
import logging
import time

settings = read_json('settings.json')
rates = settings['REFRESH_RATES']
_gitter_key = read_json('api_keys.json')['GITTER']
_gitter_room_id = settings['GITTER_ROOM_ID']
_api = settings['API_PATH']
_limit = settings["GITTER_USER_LIMIT"]
_reload_rate = rates[min(rates, key=rates.get)]
empty_api = {
	"LAST_UPDATED" : {
		"FCC_ABOUT" : None,
		"GITTER" : None
	},
	"CAMPERS" : {}
}
if os.path.isfile(_api):
	_api_output = read_json(_api)
else:
	_api_output = empty_api
	save_json(_api, _api_output)


checks = {
	'GITTER' : {
		"URL" : "https://api.gitter.im/v1/rooms/56f9df0785d51f252abb4f57/users?limit=",
	},
	'FCC_ABOUT' : {
		"URL" : "https://www.freecodecamp.com/api/users/about?username="
	},
	'GITHUB' : {
		"URL" : "https://api.github.com/users/"
	}
}
# for k,v in checks.items():
# 	checks[k]["RATE"] = rates[k]
# 	checks[k]['TIMER'] = time.perf_counter()


# "bob" : {
# 	"display" : '',
# 	"username" : 'bob',
# 	"points" : -1,
# 	"avatar" : null,
# 	"created" : null,
# 	"daysOld" : null
# }

# should set up sign-in type thing
# OAuth for Github/Twitter/Gitter/FCC(ad-hoc via challenge solution?)
#

def its_time(source):
	if source not in empty_api['LAST_UPDATED']:
		raise RuntimeException('we are not timing the "{}" api source'.format(source))
	if _api_output['LAST_UPDATED'][source] is None:
		return True
	past = datetime.datetime.strptime(_api_output['LAST_UPDATED'][source],"%Y-%m-%dT%H:%M:%S.%f")
	scheduled = past + datetime.timedelta(seconds=rates[source])
	now = datetime.datetime.utcnow()
	return now > scheduled

async def update_api():
	while True:
		if its_time('GITTER'):
			await _update_new_members()
			print('update_memebrs')
		# if its_time('GITHUB'):  # we never have to check github. avatars are linked. they auto-update.
		# 	await _update_		  # created times never change. only need to check on 1st appearance
		if its_time('FCC_ABOUT'):
			await _update_all_points()
			print('update_poitns')
		await asyncio.sleep(_reload_rate)  # conservative

async def _update_new_members(limit=None):
	global _limit
	if limit is None:
		limit = _limit
	url = "https://api.gitter.im/v1/rooms/{}/users?limit={}".format(_gitter_room_id,limit)
	first_time = _api_output == empty_api
	auth = {'Authorization' : 'Bearer {}'.format(_gitter_key)}
	# get users
	while True:
		logging.info('try to get new users')
		try:
			resp = await get_api(url, auth)
		except:
			logging.error('Failed to request '+url)
			if first_time:
				await asyncio.sleep(10)
				return await _update_new_members()  # super lazy retry. crash on overflow
			return False
		if len(resp) < limit-1:  # too lazy to check if api is inclusive
			break
		# reaching user limit
		try:
			resp = await get_api("https://api.gitter.im/v1/rooms/"+_gitter_room_id, auth)
		except:
			limit *= 2
		else:
			limit = resp['userCount']*2
		settings["GITTER_USER_LIMIT"] = _limit = limit
		save_json("settings.json",settings)

	# update api data
	# use gather, if created is null, recheck.
	# if rate limit message, stop checking until unratelimit
	to_check = []
	for member in resp:  # would be quicker to do this async.gather'd but lazy. wouldn't make much difference thinking of time frames
		logging.info('getting '+member['username'])
		shape = {
			# 'id' : member['id'],
			'username' : member['username'],  # basing gitter lookup on this for now. switch when OAuth
			'display' : member['displayName'],
			'avatar' : member['avatarUrlMedium']
		}
		camper = _api_output['CAMPERS'].setdefault(member['username'],{})
		camper.update(shape)
		camper.setdefault('points',-1)
		if not camper.get('created',None):
			to_check.append(camper['username'])

	# update people missing created dates
	await _update_dates_locally(to_check)

	# update timestamp
	now = datetime.datetime.utcnow().isoformat()
	_api_output["LAST_UPDATED"]["GITTER"] = now
	save_json(_api, _api_output)

# note doesn't save api
async def _update_dates_locally(usernames):
	resp = await asyncio.gather(*[_get_github(username) for
			username in usernames], return_exceptions=True)

	for camper in resp:
		_api_output['CAMPERS'][camper['username']]['created'] = camper['created']

async def _update_all_points():
	resp = await asyncio.gather(*[_get_points(username) for username in
			_api_output["CAMPERS"]], return_exceptions=True)

	for camper in resp:
		_api_output['CAMPERS'][camper['username']]['points'] = camper['points']
	_api_output['LAST_UPDATED']['FCC_ABOUT'] = datetime.datetime.utcnow().isoformat()
	save_json(_api, _api_output)


# returns -2 if api returns an error
# returns -3 if api returns but points are missing
# returns -4 if api call fails
async def _get_points(username):
	url = "https://www.freecodecamp.com/api/users/about?username="+username.lower()
	try:
		data = await get_api(url)
	except:
		logging.error('Failed to request '+url)
		return {'username':username, 'points':-4}

	if 'about' not in data:
		return {'username':username, 'points':-2}
	else:
		return {'username':username, 'points':data['about'].get('browniePoints',-3)}

_github_ratelimit_reset = None
# created returned as None if call failed for any reason
async def _get_github(username):
	default = {'username':username, 'created':None}
	if still_ratelimited():
		return default
	url = "https://api.github.com/users/"+username
	try:
		data = await get_api(url)
	except:
		logging.error('Failed to request '+url)
		return default
	if "created_at" not in data:
		if not await get_ratelimit(url):
			logging.error('Github api missing created_at: {}'.format(data))
		return default
	return {'username':username, 'created':data.get('created_at')}

async def get_ratelimit(url):
	global _github_ratelimit_reset
	async with aiohttp.get(url) as r:
		resp = r;
		headers = r.headers
	if headers['X-RateLimit-Remaining'] == 0:
		_github_ratelimit_reset = headers['X-RateLimit-Reset']
		logging.warning('RATE LIMIT from {} for {} minutes'.format(url,(_github_ratelimit_reset - time.time())/60))
		return False
	return True

def still_ratelimited():
	global _github_ratelimit_reset
	if _github_ratelimit_reset is None:
		return False
	if time.time() > _github_ratelimit_reset:
		_github_ratelimit_reset = None

# should hand rate limits in here since we get the json here..
async def get_api(url, headers=None):
    """
    Returns the JSON from an URL.
    Expects the url to be valid and return a JSON object.
    """
    async with aiohttp.get(url, headers=headers) as r:
        result = await r.json()
    return result


loop = asyncio.get_event_loop()
loop.run_until_complete(update_api())
loop.close()
