"""Web frontend for motor-blog: actually show web pages to visitors
"""

import datetime
import email.utils
import functools
import time

import tornado.web
from tornado import gen
from tornado.options import options as opts
import motor
from gridfs import NoFile
from werkzeug.contrib.atom import AtomFeed

from motor_blog.models import Post, Category
from motor_blog import cache, models
from motor_blog.text.link import absolute
from motor_blog.web.mobile import MobileMixin
from motor_blog.web.lytics import ga_track_event_url


__all__ = (
    # Web
    'HomeHandler', 'PostHandler', 'AllPostsHandler',
    'CategoryHandler', 'TagHandler', 'SearchHandler',

    # Atom
    'FeedHandler',
)

# TODO: cache-control headers

class MotorBlogHandler(MobileMixin, tornado.web.RequestHandler):
    def initialize(self, **kwargs):
        super(MotorBlogHandler, self).initialize(**kwargs)
        self.etag = None
        self.categories = []
        self.db = self.settings['db']

    def get_template_namespace(self):
        ns = super(MotorBlogHandler, self).get_template_namespace()

        def get_setting(setting_name):
            return self.application.settings[setting_name]

        ns.update({'setting': get_setting, 'categories': self.categories})
        return ns

    def head(self, *args, **kwargs):
        # We need to generate the full content for a HEAD request in order
        # to calculate Content-Length. Tornado knows this is a HEAD and omits
        # the content.
        self.get(*args, **kwargs)

    def get_current_user(self):
        """Logged-in username or None"""
        return self.get_secure_cookie('auth')

    def get_login_url(self):
        return self.reverse_url('login')

    @cache.cached(key='categories', invalidate_event='categories_changed')
    @gen.engine
    def get_categories(self, callback):
        # This odd control flow ensures we don't confuse exceptions thrown
        # by find() with exceptions thrown by the callback
        category_docs = None
        try:
            category_docs = yield motor.Op(
                self.db.categories.find().sort('name').to_list)

        except Exception, e:
            callback(None, e)
            return

        callback(category_docs, None)

    def get_posts(self, *args, **kwargs):
        raise NotImplementedError()

    def compute_etag(self):
        # Don't waste time md5summing the output, we'll rely on the
        # Last-Modified header
        # TODO: what's the cost?
        return None


# TODO: ample documentation, refactor
def check_last_modified(get):
    @functools.wraps(get)
    @tornado.web.asynchronous
    @gen.engine
    def _get(self, *args, **kwargs):
        categorydocs = yield motor.Op(self.get_categories)
        self.categories = categories = [Category(**doc) for doc in categorydocs]

        postdocs = yield motor.Op(self.get_posts, *args, **kwargs)
        self.posts = posts = [
            Post(**doc) if doc else None
            for doc in postdocs]

        if posts or categories:
            mod = max(
                thing.last_modified
                for things in (posts, categories)
                for thing in things if thing)

            # If-Modified-Since header is only good to the second. Truncate
            # our own mod-date to match its precision.
            mod = mod.replace(microsecond=0)
            self.set_header('Last-Modified', mod)

            # Adapted from StaticFileHandler
            ims_value = self.request.headers.get("If-Modified-Since")
            if ims_value is not None:
                date_tuple = email.utils.parsedate(ims_value)
                if_since = models.utc_tz.localize(
                    datetime.datetime.fromtimestamp(time.mktime(date_tuple)))
                if if_since >= mod:
                    # No change since client's last request. Tornado will take
                    # care of the rest.
                    self.set_status(304)
                    self.finish()
                    return

        gen.engine(get)(self, *args, **kwargs)

    return _get


class HomeHandler(MotorBlogHandler):
    def get_posts(self, callback, page_num=0):
        (self.db.posts.find(
            {'status': 'publish', 'type': 'post'},
            {'original': False},
        ).sort([('pub_date', -1)])
        .skip(int(page_num) * 10)
        .limit(10)
        .to_list(callback=callback))

    @tornado.web.addslash
    @check_last_modified
    def get(self, page_num=0):
        self.render('home.html',
            posts=self.posts, categories=self.categories,
            page_num=int(page_num))


class AllPostsHandler(MotorBlogHandler):
    def get_posts(self, callback):
        (self.db.posts.find(
            {'status': 'publish', 'type': 'post'},
            {'display': False, 'original': False},
        )
        .sort([('pub_date', -1)])
        .to_list(callback=callback))

    @tornado.web.addslash
    @check_last_modified
    def get(self):
        self.render('all-posts.html',
            posts=self.posts, categories=self.categories)

# Doesn't calculate last-modified. Saved for performance comparison.
#class AllPostsHandler(MotorBlogHandler):
#    @tornado.web.asynchronous
#    @gen.engine
#    @tornado.web.addslash
#    def get(self):
#        postdocs = yield motor.Op(
#            self.db.posts.find(
#                {'status': 'publish', 'type': 'post'},
#                {'display': False, 'original': False},
#            )
#            .sort([('pub_date', -1)])
#            .to_list)
#
#        posts = [Post(**postdoc) for postdoc in postdocs]
#        categories = yield motor.Op(get_categories, self.db)
#        categories = [Category(**doc) for doc in categories]
#
#        mod = max(
#            max(
#                thing.date_created
#                    for things in (posts, categories)
#                    for thing in things
#            ),
#            max(post.mod for post in posts)
#        )
#
#        self.render(
#            'all-posts.html',
#            posts=posts, categories=categories)


class PostHandler(MotorBlogHandler):
    """Show a single blog post or page"""
    @gen.engine
    def get_posts(self, slug, callback):
        slug = slug.rstrip('/')
        posts = self.db.posts
        postdoc = yield motor.Op(posts.find_one,
            {'slug': slug, 'status': 'publish'},
            {'summary': False, 'original': False})

        if not postdoc:
            raise tornado.web.HTTPError(404)

        if postdoc['type'] == 'redirect':
            # This redirect marks where a real post or page used to be.
            # Send the client there. Note we don't run the callback; we're
            # done.
            # TODO: actually, we *do* run the callback, is that correct?
            url = self.reverse_url('post', postdoc['redirect'])
            self.redirect(url, permanent=True)

        # Only posts have prev / next navigation, not pages
        elif postdoc['type'] == 'post':
            fields = {'summary': False, 'body': False, 'original': False}
            # TODO: this will break if drafts are published out of the order
            #   they were created; make a real publish-date and use that
            posts.find_one(
                {
                    'status': 'publish', 'type': 'post',
                    'pub_date': {'$lt': postdoc['pub_date']}
                },
                fields,
                sort=[('pub_date', -1)],
                callback=(yield gen.Callback('prevdoc')))

            posts.find_one(
                {
                    'status': 'publish', 'type': 'post',
                    'pub_date': {'$gt': postdoc['pub_date']}
                },
                fields,
                sort=[('pub_date', 1)],
                callback=(yield gen.Callback('nextdoc')))

            # Overkill for this case, but in theory we reduce latency by
            # querying for previous and next posts at once, and wait for both
            prevdoc, nextdoc = yield motor.WaitAllOps(['prevdoc', 'nextdoc'])
        else:
            prevdoc, nextdoc = None, None

        # Done
        callback([prevdoc, postdoc, nextdoc], None)

    @tornado.web.addslash
    @check_last_modified
    def get(self, slug):
        prev, post, next = self.posts
        self.render(
            'single.html',
            post=post, prev=prev, next=next, categories=self.categories)


class CategoryHandler(MotorBlogHandler):
    """Page of posts for a category"""
    def get_posts(self, callback, slug, page_num=0):
        page_num = int(page_num)
        slug = slug.rstrip('/')
        self.db.posts.find({
            'status': 'publish',
            'type': 'post',
            'categories.slug': slug,
        }, {
            'summary': False, 'original': False
        }).sort([('pub_date', -1)]
        ).skip(page_num * 10).limit(10).to_list(callback=callback)

    @tornado.web.addslash
    @check_last_modified
    def get(self, slug, page_num=0):
        page_num = int(page_num)
        slug = slug.rstrip('/')
        for this_category in self.categories:
            if this_category.slug == slug:
                break
        else:
            raise tornado.web.HTTPError(404)

        self.render('category.html',
            posts=self.posts, categories=self.categories,
            this_category=this_category, page_num=page_num)


# TODO: move to feed.py
class FeedHandler(MotorBlogHandler):
    def get_posts(self, callback, slug=None):
        query = {'status': 'publish', 'type': 'post'}

        if slug:
            slug = slug.rstrip('/')
            query['categories.slug'] = slug

        (self.db.posts.find(
            query,
            {'summary': False, 'original': False},
        ).sort([('pub_date', -1)])
        .limit(20)
        .to_list(callback=callback))

    @check_last_modified
    def get(self, slug=None):
        if slug:
            slug = slug.rstrip('/')

        this_category = None
        if slug:
            # Get all the categories and search for one with the right slug,
            # instead of actually querying for the right category, since
            # get_categories() is cached.
            slug = slug.rstrip('/')
            for category in self.categories:
                if category.slug == slug:
                    this_category = category
                    break
            else:
                raise tornado.web.HTTPError(404)

        title = opts.blog_name

        if this_category:
            title = '%s - Posts about %s' % (title, this_category.name)

        author = {'name': opts.author_display_name, 'email': opts.author_email}
        if this_category:
            feed_url = absolute(
                self.reverse_url('category-feed', this_category.slug))
        else:
            feed_url = absolute(self.reverse_url('feed'))

        if self.posts:
            updated = max(max(p.mod, p.date_created) for p in self.posts)
        else:
            updated = datetime.datetime.now(tz=self.application.settings['tz'])

        referer = self.request.headers.get('referer', '-') # (sic)

        feed = AtomFeed(
            title=title,
            feed_url=feed_url,
            url=absolute(self.reverse_url('home')),
            author=author,
            updated=updated,
            # TODO: customizable icon, also a 'logo' kwarg
            icon=absolute(self.reverse_url('theme-static', '/theme/static/square96.png')),
            generator=('Motor-Blog', 'https://github.com/ajdavis/motor-blog', '0.1'),
        )

        for post in self.posts:
            url = absolute(self.reverse_url('post', post.slug))
            body = post.body + '<img src="%s" width="1px" height="1px">' % ga_track_event_url(
                path=url,
                title=post.title,
                category_name=this_category.name if this_category else 'unknown',
                referer=referer
            )

            feed.add(
                title=post.title,
                content=body,
                content_type='html',
                summary=post.summary,
                author=author,
                url=url,
                id=url,
                published=post.date_created,
                # Don't update 'updated' - it seems to make Planet Python re-post my updated items,
                #    which is spammy.
                #updated=post.mod,
                updated=post.date_created,
            )

        self.set_header('Content-Type', 'application/atom+xml; charset=UTF-8')
        self.write(unicode(feed))
        self.finish()


class TagHandler(MotorBlogHandler):
    """Page of posts for a tag"""
    def get_posts(self, callback, tag, page_num=0):
        page_num = int(page_num)
        tag = tag.rstrip('/')
        self.db.posts.find({
            'status': 'publish',
            'type': 'post',
            'tags': tag,
        }, {
            'summary': False, 'original': False
        }).sort([('pub_date', -1)]).skip(page_num * 10).limit(10).to_list(callback=callback)

    @tornado.web.addslash
    @check_last_modified
    def get(self, tag, page_num=0):
        page_num = int(page_num)
        tag = tag.rstrip('/')
        self.render('tag.html',
            posts=self.posts, categories=self.categories,
            this_tag=tag, page_num=page_num)


class SearchHandler(MotorBlogHandler):
    @tornado.web.asynchronous
    @gen.engine
    def get(self):
        # TODO: refactor with check_last_modified(), this is gross
        #   we need an async version of RequestHandler.prepare()
        categorydocs = yield motor.Op(self.get_categories)
        self.categories = [Category(**doc) for doc in categorydocs]

        q = self.get_argument('q', None)
        if q:
            response = yield motor.Op(self.db.command, 'text', 'posts',
                search=q,
                filter={'status': 'publish', 'type': 'post'},
                projection={
                    'display': False, 'original': False, 'plain': False},
                limit=50)

            posts = [Post(**result['obj']) for result in response['results']]
        else:
            posts = []
        self.render('search.html', q=q, posts=posts)
