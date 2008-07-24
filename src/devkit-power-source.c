/* -*- Mode: C; tab-width: 8; indent-tabs-mode: nil; c-basic-offset: 8 -*-
 *
 * Copyright (C) 2008 David Zeuthen <david@fubar.dk>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
 *
 */

#ifdef HAVE_CONFIG_H
#  include "config.h"
#endif

#include <string.h>
#include <math.h>

#include <glib.h>
#include <glib/gstdio.h>
#include <glib/gi18n-lib.h>
#include <glib-object.h>
#include <dbus/dbus-glib.h>
#include <dbus/dbus-glib-lowlevel.h>
#include <devkit-gobject.h>
#include <polkit-dbus/polkit-dbus.h>

#include "sysfs-utils.h"
#include "devkit-power-enum.h"
#include "devkit-power-source.h"
#include "devkit-power-marshal.h"

/*--------------------------------------------------------------------------------------------------------------*/
#include "devkit-power-source-glue.h"

#define DK_POWER_MIN_CHARGED_PERCENTAGE	60

struct DevkitPowerSourcePrivate
{
        DBusGConnection *system_bus_connection;
        DBusGProxy      *system_bus_proxy;
        DevkitPowerDaemon *daemon;
        DevkitDevice *d;

        char *object_path;
        char *native_path;

        guint poll_timer_id;

        char *vendor;
        char *model;
        char *serial;
        GTimeVal update_time;
        DevkitPowerType type;

        gboolean line_power_online;
        DevkitPowerState battery_state;
        DevkitPowerTechnology battery_technology;

        double battery_energy;
        double battery_energy_empty;
        double battery_energy_full;
        double battery_energy_full_design;
        double battery_energy_rate;
        gint64 battery_time_to_empty;
        gint64 battery_time_to_full;
        double battery_percentage;
};

static void     devkit_power_source_class_init  (DevkitPowerSourceClass *klass);
static void     devkit_power_source_init        (DevkitPowerSource      *seat);
static void     devkit_power_source_finalize    (GObject     *object);

static gboolean update (DevkitPowerSource *source);

enum
{
        PROP_0,
        PROP_NATIVE_PATH,
        PROP_VENDOR,
        PROP_MODEL,
        PROP_SERIAL,
        PROP_UPDATE_TIME,
        PROP_TYPE,
        PROP_LINE_POWER_ONLINE,
        PROP_BATTERY_STATE,
        PROP_BATTERY_ENERGY,
        PROP_BATTERY_ENERGY_EMPTY,
        PROP_BATTERY_ENERGY_FULL,
        PROP_BATTERY_ENERGY_FULL_DESIGN,
        PROP_BATTERY_ENERGY_RATE,
        PROP_BATTERY_TIME_TO_EMPTY,
        PROP_BATTERY_TIME_TO_FULL,
        PROP_BATTERY_PERCENTAGE,
        PROP_BATTERY_TECHNOLOGY,
};

enum
{
        CHANGED_SIGNAL,
        LAST_SIGNAL,
};

static guint signals[LAST_SIGNAL] = { 0 };

G_DEFINE_TYPE (DevkitPowerSource, devkit_power_source, DEVKIT_TYPE_POWER_DEVICE)
#define DEVKIT_POWER_SOURCE_GET_PRIVATE(o) (G_TYPE_INSTANCE_GET_PRIVATE ((o), DEVKIT_TYPE_POWER_SOURCE, DevkitPowerSourcePrivate))

static const char *devkit_power_source_get_object_path (DevkitPowerDevice *device);
static void        devkit_power_source_removed         (DevkitPowerDevice *device);
static gboolean    devkit_power_source_changed         (DevkitPowerDevice *device,
                                                         DevkitDevice      *d,
                                                         gboolean           synthesized);

static void
get_property (GObject         *object,
              guint            prop_id,
              GValue          *value,
              GParamSpec      *pspec)
{
        DevkitPowerSource *source = DEVKIT_POWER_SOURCE (object);

        switch (prop_id) {
        case PROP_NATIVE_PATH:
                g_value_set_string (value, source->priv->native_path);
                break;
        case PROP_VENDOR:
                g_value_set_string (value, source->priv->vendor);
                break;
        case PROP_MODEL:
                g_value_set_string (value, source->priv->model);
                break;
        case PROP_SERIAL:
                g_value_set_string (value, source->priv->serial);
                break;
        case PROP_UPDATE_TIME:
                g_value_set_uint64 (value, source->priv->update_time.tv_sec);
                break;
        case PROP_TYPE:
                g_value_set_string (value, devkit_power_convert_type_to_text (source->priv->type));
                break;

        case PROP_LINE_POWER_ONLINE:
                g_value_set_boolean (value, source->priv->line_power_online);
                break;
        case PROP_BATTERY_STATE:
                g_value_set_string (value, devkit_power_convert_state_to_text (source->priv->battery_state));
                break;
        case PROP_BATTERY_ENERGY:
                g_value_set_double (value, source->priv->battery_energy);
                break;
        case PROP_BATTERY_ENERGY_EMPTY:
                g_value_set_double (value, source->priv->battery_energy_empty);
                break;
        case PROP_BATTERY_ENERGY_FULL:
                g_value_set_double (value, source->priv->battery_energy_full);
                break;
        case PROP_BATTERY_ENERGY_FULL_DESIGN:
                g_value_set_double (value, source->priv->battery_energy_full_design);
                break;
        case PROP_BATTERY_ENERGY_RATE:
                g_value_set_double (value, source->priv->battery_energy_rate);
                break;
        case PROP_BATTERY_TIME_TO_EMPTY:
                g_value_set_int64 (value, source->priv->battery_time_to_empty);
                break;
        case PROP_BATTERY_TIME_TO_FULL:
                g_value_set_int64 (value, source->priv->battery_time_to_full);
                break;
        case PROP_BATTERY_PERCENTAGE:
                g_value_set_double (value, source->priv->battery_percentage);
                break;

        case PROP_BATTERY_TECHNOLOGY:
                g_value_set_string (value, devkit_power_convert_technology_to_text (source->priv->battery_technology));
                break;

        default:
                G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
                break;
        }
}



static void
devkit_power_source_class_init (DevkitPowerSourceClass *klass)
{
        GObjectClass           *object_class = G_OBJECT_CLASS (klass);
        DevkitPowerDeviceClass *device_class = DEVKIT_POWER_DEVICE_CLASS (klass);

        object_class->finalize = devkit_power_source_finalize;
        object_class->get_property = get_property;
        device_class->changed = devkit_power_source_changed;
        device_class->removed = devkit_power_source_removed;
        device_class->get_object_path = devkit_power_source_get_object_path;

        g_type_class_add_private (klass, sizeof (DevkitPowerSourcePrivate));

        signals[CHANGED_SIGNAL] =
                g_signal_new ("changed",
                              G_OBJECT_CLASS_TYPE (klass),
                              G_SIGNAL_RUN_LAST | G_SIGNAL_DETAILED,
                              0,
                              NULL, NULL,
                              g_cclosure_marshal_VOID__VOID,
                              G_TYPE_NONE, 0);

        dbus_g_object_type_install_info (DEVKIT_TYPE_POWER_SOURCE, &dbus_glib_devkit_power_source_object_info);

        g_object_class_install_property (
                object_class,
                PROP_NATIVE_PATH,
                g_param_spec_string ("native-path", NULL, NULL, NULL, G_PARAM_READABLE));

        g_object_class_install_property (
                object_class,
                PROP_VENDOR,
                g_param_spec_string ("vendor", NULL, NULL, NULL, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_MODEL,
                g_param_spec_string ("model", NULL, NULL, NULL, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_SERIAL,
                g_param_spec_string ("serial", NULL, NULL, NULL, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_UPDATE_TIME,
                g_param_spec_uint64 ("update-time", NULL, NULL, 0, G_MAXUINT64, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_TYPE,
                g_param_spec_string ("type", NULL, NULL, NULL, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_LINE_POWER_ONLINE,
                g_param_spec_boolean ("line-power-online", NULL, NULL, FALSE, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_ENERGY,
                g_param_spec_double ("battery-energy", NULL, NULL, 0, G_MAXDOUBLE, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_STATE,
                g_param_spec_string ("battery-state", NULL, NULL, NULL, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_ENERGY_EMPTY,
                g_param_spec_double ("battery-energy-empty", NULL, NULL, 0, G_MAXDOUBLE, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_ENERGY_FULL,
                g_param_spec_double ("battery-energy-full", NULL, NULL, 0, G_MAXDOUBLE, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_ENERGY_FULL_DESIGN,
                g_param_spec_double ("battery-energy-full-design", NULL, NULL, 0, G_MAXDOUBLE, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_ENERGY_RATE,
                g_param_spec_double ("battery-energy-rate", NULL, NULL, -G_MAXDOUBLE, G_MAXDOUBLE, 0, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_TIME_TO_EMPTY,
                g_param_spec_int64 ("battery-time-to-empty", NULL, NULL, -1, G_MAXINT64, -1, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_TIME_TO_FULL,
                g_param_spec_int64 ("battery-time-to-full", NULL, NULL, -1, G_MAXINT64, -1, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_PERCENTAGE,
                g_param_spec_double ("battery-percentage", NULL, NULL, -1, 100, -1, G_PARAM_READABLE));
        g_object_class_install_property (
                object_class,
                PROP_BATTERY_TECHNOLOGY,
                g_param_spec_string ("battery-technology", NULL, NULL, NULL, G_PARAM_READABLE));
}

static void
devkit_power_source_init (DevkitPowerSource *source)
{
        source->priv = DEVKIT_POWER_SOURCE_GET_PRIVATE (source);
        source->priv->battery_time_to_empty = -1;
        source->priv->battery_time_to_full = -1;
}

static void
devkit_power_source_finalize (GObject *object)
{
        DevkitPowerSource *source;

        g_return_if_fail (object != NULL);
        g_return_if_fail (DEVKIT_IS_POWER_SOURCE (object));

        source = DEVKIT_POWER_SOURCE (object);
        g_return_if_fail (source->priv != NULL);

        g_object_unref (source->priv->d);
        g_object_unref (source->priv->daemon);

        g_free (source->priv->native_path);

        g_free (source->priv->vendor);
        g_free (source->priv->model);
        g_free (source->priv->serial);

        if (source->priv->poll_timer_id > 0)
                g_source_remove (source->priv->poll_timer_id);

        G_OBJECT_CLASS (devkit_power_source_parent_class)->finalize (object);
}

static char *
compute_object_path_from_basename (const char *native_path_basename)
{
        char *basename;
        char *object_path;
        unsigned int n;

        /* TODO: need to be more thorough with making proper object
         * names that won't make D-Bus crash. This is just to cope
         * with dm-0...
         */
        basename = g_path_get_basename (native_path_basename);
        for (n = 0; basename[n] != '\0'; n++)
                if (basename[n] == '-')
                        basename[n] = '_';
        object_path = g_build_filename ("/sources/", basename, NULL);
        g_free (basename);

        return object_path;
}

static char *
compute_object_path (const char *native_path)
{
        char *basename;
        char *object_path;

        basename = g_path_get_basename (native_path);
        object_path = compute_object_path_from_basename (basename);
        g_free (basename);
        return object_path;
}

static gboolean
register_power_source (DevkitPowerSource *source)
{
        DBusConnection *connection;
        GError *error = NULL;

        source->priv->system_bus_connection = dbus_g_bus_get (DBUS_BUS_SYSTEM, &error);
        if (source->priv->system_bus_connection == NULL) {
                if (error != NULL) {
                        g_critical ("error getting system bus: %s", error->message);
                        g_error_free (error);
                }
                goto error;
        }
        connection = dbus_g_connection_get_connection (source->priv->system_bus_connection);

        source->priv->object_path = compute_object_path (source->priv->native_path);

        dbus_g_connection_register_g_object (source->priv->system_bus_connection,
                                             source->priv->object_path,
                                             G_OBJECT (source));

        source->priv->system_bus_proxy = dbus_g_proxy_new_for_name (source->priv->system_bus_connection,
                                                                    DBUS_SERVICE_DBUS,
                                                                    DBUS_PATH_DBUS,
                                                                    DBUS_INTERFACE_DBUS);

        return TRUE;

error:
        return FALSE;
}

DevkitPowerSource *
devkit_power_source_new (DevkitPowerDaemon *daemon, DevkitDevice *d)
{
        DevkitPowerSource *source;
        const char *native_path;

        source = NULL;
        native_path = devkit_device_get_native_path (d);

        source = DEVKIT_POWER_SOURCE (g_object_new (DEVKIT_TYPE_POWER_SOURCE, NULL));
        source->priv->d = g_object_ref (d);
        source->priv->daemon = g_object_ref (daemon);
        source->priv->native_path = g_strdup (native_path);

        if (sysfs_file_exists (native_path, "online")) {
                source->priv->type = DEVKIT_POWER_TYPE_LINE_POWER;
        } else {
                /* this is correct, UPS and CSR are not in the kernel */
                source->priv->type = DEVKIT_POWER_TYPE_BATTERY;
        }

        if (!update (source)) {
                g_object_unref (source);
                source = NULL;
                goto out;
        }

        if (! register_power_source (DEVKIT_POWER_SOURCE (source))) {
                g_object_unref (source);
                source = NULL;
                goto out;
        }

out:
        return source;
}

static void
emit_changed (DevkitPowerSource *source)
{
        g_print ("emitting changed on %s\n", source->priv->native_path);
        g_signal_emit_by_name (source->priv->daemon,
                               "device-changed",
                               source->priv->object_path,
                               NULL);
        g_signal_emit (source, signals[CHANGED_SIGNAL], 0);
}

static gboolean
devkit_power_source_changed (DevkitPowerDevice *device, DevkitDevice *d, gboolean synthesized)
{
        DevkitPowerSource *source = DEVKIT_POWER_SOURCE (device);
        gboolean keep_source;

        g_object_unref (source->priv->d);
        source->priv->d = g_object_ref (d);

        keep_source = update (source);

        /* this 'change' event might prompt us to remove the source */
        if (!keep_source)
                goto out;

        /* no, it's good .. keep it */
        emit_changed (source);

out:
        return keep_source;
}

void
devkit_power_source_removed (DevkitPowerDevice *device)
{
}

static const char *
devkit_power_source_get_object_path (DevkitPowerDevice *device)
{
        DevkitPowerSource *source = DEVKIT_POWER_SOURCE (device);
        return source->priv->object_path;
}

/*--------------------------------------------------------------------------------------------------------------*/

static gboolean
update_line_power (DevkitPowerSource *source)
{
        source->priv->line_power_online = sysfs_get_int (source->priv->native_path, "online");
        return TRUE;
}

static gboolean
update_battery (DevkitPowerSource *source)
{
        char *status;
        gboolean is_charging;
        gboolean is_discharging;

        /* TODO: this needs to handle lots of special cases when certain
         *       files exist, it needs to prefer _avg to _now etc. etc. etc.
         *
         *       This is just a very quick hack for now.
         */

        status = g_strstrip (sysfs_get_string (source->priv->native_path, "status"));
        is_charging = strcasecmp (status, "charging") == 0;
        is_discharging = strcasecmp (status, "discharging") == 0;

        source->priv->battery_energy =
                sysfs_get_double (source->priv->native_path, "energy_now") / 1000000.0;
        source->priv->battery_energy_full =
                sysfs_get_double (source->priv->native_path, "energy_full") / 1000000.0;
        source->priv->battery_energy_full_design =
                sysfs_get_double (source->priv->native_path, "energy_full_design") / 1000000.0;
        source->priv->battery_energy_rate =
                fabs (sysfs_get_double (source->priv->native_path, "current_now") / 1000000.0);
        if (is_charging)
                source->priv->battery_energy_rate *= -1.0;

        /* get a precise percentage */
        source->priv->battery_percentage = 100.0 * source->priv->battery_energy / source->priv->battery_energy_full;
        if (source->priv->battery_percentage < 0)
                source->priv->battery_percentage = 0;
        if (source->priv->battery_percentage > 100.0)
                source->priv->battery_percentage = 100.0;

        /* get the state */
        if (is_charging)
                source->priv->battery_state = DEVKIT_POWER_STATE_CHARGING;
        else if (is_discharging)
                source->priv->battery_state = DEVKIT_POWER_STATE_DISCHARGING;
        else if (source->priv->battery_percentage > DK_POWER_MIN_CHARGED_PERCENTAGE)
                source->priv->battery_state = DEVKIT_POWER_STATE_FULLY_CHARGED;
        else
                source->priv->battery_state = DEVKIT_POWER_STATE_EMPTY;

        g_free (status);
        return TRUE;
}

static gboolean
_poll_battery (DevkitPowerSource *source)
{
        g_warning ("No updates on source %s for 30 seconds; forcing update", source->priv->native_path);
        source->priv->poll_timer_id = 0;
        update (source);
        emit_changed (source);
        return FALSE;
}

static gboolean
update (DevkitPowerSource *source)
{
        gboolean ret;

        if (source->priv->poll_timer_id > 0) {
                g_source_remove (source->priv->poll_timer_id);
                source->priv->poll_timer_id = 0;
        }

        /* initial values */
        if (source->priv->vendor == NULL) {
                char *s;

                s = g_strstrip (sysfs_get_string (source->priv->native_path, "technology"));
                source->priv->battery_technology = devkit_power_convert_acpi_technology_to_enum (s);
                g_free (s);

                source->priv->vendor = g_strstrip (sysfs_get_string (source->priv->native_path, "manufacturer"));
                source->priv->model = g_strstrip (sysfs_get_string (source->priv->native_path, "model_name"));
                source->priv->serial = g_strstrip (sysfs_get_string (source->priv->native_path, "serial_number"));
        }

        g_get_current_time (&(source->priv->update_time));

        switch (source->priv->type) {
        case DEVKIT_POWER_TYPE_LINE_POWER:
                ret = update_line_power (source);
                break;
        case DEVKIT_POWER_TYPE_BATTERY:

                ret = update_battery (source);

                /* Seems that we don't get change uevents from the
                 * kernel on some BIOS types; set up a timer to poll
                 *
                 * TODO: perhaps only do this if we do not get frequent updates.
                 */
                source->priv->poll_timer_id = g_timeout_add_seconds (30, (GSourceFunc) _poll_battery, source);

                break;
        default:
                g_assert_not_reached ();
                break;
        }

        return ret;
}

/*--------------------------------------------------------------------------------------------------------------*/

gboolean
devkit_power_source_refresh (DevkitPowerSource     *power_source,
                             DBusGMethodInvocation *context)
{
        update (power_source);
        dbus_g_method_return (context);
        return TRUE;
}
